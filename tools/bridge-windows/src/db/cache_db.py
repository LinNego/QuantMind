"""
通达信数据缓存层 - SQLite

把从通达信拉取的数据 (K线/股票信息/财务/行情快照) 持久化到本地 SQLite,
供后续随时查询, 减少重复连通达信, 数据可追溯。

设计:
- 单文件 SQLite (data/tdx_cache.db), 零配置, Windows 原生支持
- 每种数据一张表, 按 symbol+date 做唯一键
- 自动建表, 写入 upsert
- 提供最近 N 条 / 区间查询
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)


class CacheDb:
    """SQLite 缓存, 线程安全 (桥内多线程访问)."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = None
        try:
            self._conn = self._connect()
        except sqlite3.DatabaseError as exc:
            log.critical("缓存数据库无法打开(%s)，隔离重建: %s", exc, self.db_path)
        # 启动自检：缓存库是可丢弃的数据，损坏就隔离重建，绝不允许带着
        # "database disk image is malformed" 一直跑到收盘（2026-10-23 生产
        # 9.7GB 库损坏，所有缓存/审计写失败，每笔调用刷一条 traceback）。
        # 这里只做**廉价**检查（连接 + sqlite_master）；全量 quick_check 会
        # 扫整库，9GB 要几分钟，绝不能挡启动——交给后台维护线程
        # （heal_if_corrupt）和写入路径的 malformed 自愈。
        if self._conn is None or not self._quick_schema_ok():
            if self._conn is not None:
                log.critical("缓存数据库损坏，隔离旧文件并重建: %s", self.db_path)
            self._quarantine_locked()
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        # 空闲页自动回收（新库生效；旧库需 VACUUM 才能切换模式，保持原样即可）
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        # WAL 到 64MB 就 checkpoint，避免 -wal 无限增长
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute("PRAGMA wal_autocheckpoint=2000")
        return conn

    def _quick_schema_ok(self) -> bool:
        """廉价可用性检查（不扫全库）：能读 sqlite_master 即认为结构可用。"""
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def _integrity_ok(self) -> bool:
        if self._conn is None:
            return False
        try:
            row = self._conn.execute("PRAGMA quick_check(1)").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        except sqlite3.DatabaseError:
            return False

    def heal_if_corrupt(self) -> bool:
        """全量 quick_check（慢，仅后台线程调用）；损坏则隔离重建。

        返回 True 表示刚做过重建。启动阶段不调用，避免 9GB 库把桥挡在门外。
        检查期间**不持锁**（9GB 扫库几分钟，持锁会冻结所有缓存写）；
        重建前再确认连接没被其它线程换过，避免误隔离新库。
        """
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            ok = bool(row) and str(row[0]).lower() == "ok"
        except sqlite3.DatabaseError:
            ok = False
        if ok:
            return False
        with self._lock:
            if self._conn is not conn:
                return False
            self._quarantine_locked()
        return True

    @staticmethod
    def _is_malformed(exc: sqlite3.DatabaseError) -> bool:
        return "malformed" in str(exc).lower()

    def _quarantine_locked(self) -> None:
        """隔离损坏库并重建空库。**调用方必须已持有 self._lock**。

        缓存/审计库损坏时唯一正确的动作：关连接 → 改名留证 → 新库。
        由人工事后 `scripts/prune_cache.py` 或 forensic 工具处理旧文件。
        """
        try:
            if self._conn is not None:
                self._conn.close()
        except sqlite3.Error:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = self.db_path + suffix
            if os.path.exists(src):
                try:
                    os.replace(src, f"{src}.corrupt-{stamp}")
                except OSError as exc:
                    log.warning("隔离缓存文件失败 %s: %s", src, exc)
        self._conn = self._connect()
        self._create_tables_locked()

    def _recover(self, exc: sqlite3.DatabaseError) -> None:
        """写/读遇到 malformed：隔离重建，调用方随后重试一次。"""
        log.critical("缓存数据库损坏(%s)，隔离重建: %s", exc, self.db_path)
        with self._lock:
            self._quarantine_locked()

    def _write(self, fn) -> None:
        """执行一次写操作；malformed 时隔离重建并重试一次。"""
        try:
            with self._lock:
                fn(self._conn)
                self._conn.commit()
        except sqlite3.DatabaseError as exc:
            if not self._is_malformed(exc):
                raise
            self._recover(exc)
            with self._lock:
                fn(self._conn)
                self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- 表结构 ----

    def _create_tables(self):
        with self._lock:
            self._create_tables_locked()

    def _create_tables_locked(self):
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS kline (
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL,
                fetched_at TEXT,
                PRIMARY KEY (symbol, period, date)
            );
            CREATE TABLE IF NOT EXISTS stock_info (
                symbol TEXT PRIMARY KEY,
                data TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS market_snapshot (
                symbol TEXT PRIMARY KEY,
                data TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS financial (
                symbol TEXT NOT NULL,
                report_type TEXT,
                data TEXT,
                fetched_at TEXT,
                PRIMARY KEY (symbol, report_type)
            );
            CREATE TABLE IF NOT EXISTS sector_stocks (
                block_code TEXT PRIMARY KEY,
                stocks TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tdx_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT,
                category TEXT,
                symbol TEXT,
                side TEXT,
                volume REAL,
                price REAL,
                order_id TEXT,
                status TEXT,
                params TEXT,
                result TEXT,
                duration_ms REAL,
                error TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT,
                event TEXT,
                symbol TEXT,
                side TEXT,
                volume REAL,
                price REAL,
                order_id TEXT,
                status TEXT,
                message TEXT,
                detail TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created_at TEXT
            );
            """)
        self._conn.commit()

    # ---- 通用 upsert ----

    def _upsert(self, table: str, row: dict, conflict_cols: tuple):
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        conflict = ",".join(conflict_cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_cols)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
               f"ON CONFLICT({conflict}) DO UPDATE SET {updates}")
        values = [row[c] for c in cols]
        self._write(lambda conn: conn.execute(sql, values))

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        try:
            with self._lock:
                self._conn.row_factory = sqlite3.Row
                rows = self._conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.DatabaseError as exc:
            if not self._is_malformed(exc):
                raise
            self._recover(exc)
            with self._lock:
                self._conn.row_factory = sqlite3.Row
                rows = self._conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]

    # ---- K线 ----

    def save_kline(self, symbol: str, period: str, bars: list[dict]):
        """保存K线. bars: [{date, open, high, low, close, volume, amount}]"""
        for b in bars:
            self._upsert("kline", {
                "symbol": symbol, "period": period,
                "date": b.get("date", ""),
                "open": b.get("open"), "high": b.get("high"),
                "low": b.get("low"), "close": b.get("close"),
                "volume": b.get("volume"), "amount": b.get("amount"),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }, ("symbol", "period", "date"))

    def get_kline(self, symbol: str, period: str = "1d", limit: int = 500) -> list[dict]:
        return self._query(
            "SELECT * FROM kline WHERE symbol=? AND period=? ORDER BY date DESC LIMIT ?",
            (symbol, period, limit))

    # ---- 股票信息 / 快照 / 财务 / 板块 ----

    def save_stock_info(self, symbol: str, data: dict):
        self._upsert("stock_info", {
            "symbol": symbol, "data": json.dumps(data, ensure_ascii=False),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }, ("symbol",))

    def get_stock_info(self, symbol: str) -> Optional[dict]:
        rows = self._query("SELECT data FROM stock_info WHERE symbol=?", (symbol,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_snapshot(self, symbol: str, data: dict):
        self._upsert("market_snapshot", {
            "symbol": symbol, "data": json.dumps(data, ensure_ascii=False),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }, ("symbol",))

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        rows = self._query("SELECT data FROM market_snapshot WHERE symbol=?", (symbol,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_financial(self, symbol: str, report_type: str, data: dict):
        self._upsert("financial", {
            "symbol": symbol, "report_type": report_type,
            "data": json.dumps(data, ensure_ascii=False),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }, ("symbol", "report_type"))

    def get_financial(self, symbol: str, report_type: str = "") -> Optional[dict]:
        if report_type:
            rows = self._query("SELECT data FROM financial WHERE symbol=? AND report_type=?",
                               (symbol, report_type))
        else:
            rows = self._query("SELECT data FROM financial WHERE symbol=? ORDER BY fetched_at DESC LIMIT 1",
                               (symbol,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_sector_stocks(self, block_code: str, stocks: list):
        self._upsert("sector_stocks", {
            "block_code": block_code, "stocks": json.dumps(stocks, ensure_ascii=False),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }, ("block_code",))

    def get_sector_stocks(self, block_code: str) -> Optional[list]:
        rows = self._query("SELECT stocks FROM sector_stocks WHERE block_code=?", (block_code,))
        return json.loads(rows[0]["stocks"]) if rows else None

    # ---- 操作日志 ----

    def log_call(self, method: str, symbol: str, params: dict, result: Any,
                 category: str = "", side: str = "", volume: float = None,
                 price: float = None, order_id: str = "", status: str = "",
                 duration_ms: float = None, error: str = ""):
        """记录所有桥调用 (查询/下单/推送), 精确到耗时/状态/结果."""
        values = (method, category, symbol or "", side or "", volume, price,
                  order_id or "", status or "",
                  json.dumps(params, ensure_ascii=False, default=str)[:2000],
                  json.dumps(result, ensure_ascii=False, default=str)[:4000],
                  duration_ms, error or "",
                  datetime.now().isoformat(timespec="milliseconds"))
        self._write(lambda conn: conn.execute(
            "INSERT INTO tdx_log (method, category, symbol, side, volume, price, "
            "order_id, status, params, result, duration_ms, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values))

    def log_trade(self, plan_id: str, event: str, symbol: str, side: str,
                  volume: float, price: float, order_id: str, status: str,
                  message: str = "", detail: dict = None):
        """记录交易事件 (下单/成交/撤单/失败), 精确到委托号/状态."""
        values = (plan_id or "", event, symbol or "", side or "", volume, price,
                  order_id or "", status or "", message or "",
                  json.dumps(detail or {}, ensure_ascii=False, default=str)[:2000],
                  datetime.now().isoformat(timespec="milliseconds"))
        self._write(lambda conn: conn.execute(
            "INSERT INTO trade_log (plan_id, event, symbol, side, volume, price, "
            "order_id, status, message, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", values))

    # ---- 批量写入（后台日志线程用；一次 commit，避免每请求 fsync） ----

    def log_calls_bulk(self, records: list[dict]) -> int:
        """批量写 tdx_log；records 由桥的后台日志线程聚合（含 created_at）。"""
        if not records:
            return 0
        rows = []
        for r in records:
            try:
                params_json = json.dumps(r.get("params") or {}, ensure_ascii=False,
                                         default=str)[:2000]
                result_json = json.dumps(r.get("result"), ensure_ascii=False,
                                         default=str)[:4000]
            except (TypeError, ValueError):
                params_json, result_json = "", ""
            rows.append((
                r.get("method") or "", r.get("category") or "",
                r.get("symbol") or "", r.get("side") or "",
                r.get("volume"), r.get("price"), r.get("order_id") or "",
                r.get("status") or "", params_json, result_json,
                r.get("duration_ms"), r.get("error") or "",
                r.get("created_at") or datetime.now().isoformat(timespec="milliseconds"),
            ))
        self._write(lambda conn: conn.executemany(
            "INSERT INTO tdx_log (method, category, symbol, side, volume, price, "
            "order_id, status, params, result, duration_ms, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows))
        return len(rows)

    def log_trades_bulk(self, records: list[dict]) -> int:
        """批量写 trade_log（交易台账，优先级高于调用日志）。"""
        if not records:
            return 0
        rows = []
        for r in records:
            try:
                detail_json = json.dumps(r.get("detail") or {}, ensure_ascii=False,
                                         default=str)[:2000]
            except (TypeError, ValueError):
                detail_json = ""
            rows.append((
                r.get("plan_id") or "", r.get("event") or "",
                r.get("symbol") or "", r.get("side") or "",
                r.get("volume"), r.get("price"), r.get("order_id") or "",
                r.get("status") or "", r.get("message") or "", detail_json,
                r.get("created_at") or datetime.now().isoformat(timespec="milliseconds"),
            ))
        self._write(lambda conn: conn.executemany(
            "INSERT INTO trade_log (plan_id, event, symbol, side, volume, price, "
            "order_id, status, message, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows))
        return len(rows)

    def get_trade_logs(self, symbol: str = "", limit: int = 100) -> list[dict]:
        if symbol:
            return self._query(
                "SELECT * FROM trade_log WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit))
        return self._query("SELECT * FROM trade_log ORDER BY id DESC LIMIT ?", (limit,))

    def get_logs(self, method: str = "", limit: int = 100) -> list[dict]:
        if method:
            return self._query(
                "SELECT * FROM tdx_log WHERE method=? ORDER BY id DESC LIMIT ?", (method, limit))
        return self._query("SELECT * FROM tdx_log ORDER BY id DESC LIMIT ?", (limit,))

    def get_stats(self) -> dict:
        """返回缓存统计（独立只读连接，不持有写入锁，不阻塞事件循环的写路径）。

        旧实现 `with self._lock` 跑 7 个 COUNT(*)（tdx_log 曾 512 万行，3-52s），
        期间所有 cache 写（快照/K线）都在事件循环里等锁。现在用独立连接，
        WAL 下读写互不阻塞；连接失败再退回原路径。
        """
        allowed_tables = ("kline", "stock_info", "market_snapshot", "financial",
                          "sector_stocks", "tdx_log", "trade_log")
        counts = {}
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cur = conn.cursor()
            for table in allowed_tables:
                try:
                    counts[table] = cur.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    counts[table] = 0
            return counts
        except sqlite3.Error:
            conn = None
        finally:
            if conn is not None:
                conn.close()
        # 极端兜底：独立连接打不开时才走共享连接
        with self._lock:
            cur = self._conn.cursor()
            for table in allowed_tables:
                try:
                    counts[table] = cur.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    counts[table] = 0
        return counts

    def get_cache_disk(self) -> int:
        """缓存数据库文件大小 (字节)."""
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    # ---- 日志留存清理 ----

    def count_prune_candidates(self, tdx_retention_days: int = 30,
                               tdx_max_rows: int = 1_500_000,
                               trade_retention_days: int = 365) -> dict:
        """只统计会被清理的行数（dry-run 用，不删除任何数据）。"""
        now = datetime.now()
        tdx_cutoff = (now - timedelta(days=tdx_retention_days)).isoformat(
            timespec="milliseconds")
        trade_cutoff = (now - timedelta(days=trade_retention_days)).isoformat(
            timespec="milliseconds")
        with self._lock:
            cur = self._conn.cursor()
            tdx_total = cur.execute("SELECT COUNT(*) FROM tdx_log").fetchone()[0]
            trade_total = cur.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
            tdx_old = cur.execute(
                "SELECT COUNT(*) FROM tdx_log WHERE created_at < ?",
                (tdx_cutoff,)).fetchone()[0]
            trade_old = cur.execute(
                "SELECT COUNT(*) FROM trade_log WHERE created_at < ?",
                (trade_cutoff,)).fetchone()[0]
        tdx_remaining = max(tdx_total - tdx_old, 0)
        return {
            "tdx_log": tdx_old + max(tdx_remaining - tdx_max_rows, 0),
            "trade_log": trade_old,
            "tdx_log_total": tdx_total,
            "trade_log_total": trade_total,
        }

    def prune_logs(self, tdx_retention_days: int = 30,
                   tdx_max_rows: int = 1_500_000,
                   trade_retention_days: int = 365,
                   batch_size: int = 5000,
                   sleep_s: float = 0.01) -> dict:
        """按保留策略清理审计日志，分批提交，避免长事务锁住桥的写路径。

        策略：
        - ``tdx_log``（每次桥调用的审计行）：默认保留 30 天，且最多保留最新
          150 万行——生产曾到 777 万行 / 9.6GB，`COUNT(*)` 单次 12 秒；
        - ``trade_log``（下单/成交/撤单台账）：默认保留 365 天。

        **不在整个清理期间持有 self._lock**：每批单独加锁，让桥的
        `log_call()` 可以插队，否则一次十分钟的清理会把事件循环一起按住。

        按 id 而不是 created_at 建索引：大表建索引要长时间持锁；id 随插入
        单调递增，先扫一次找出时间阈值对应的 id 边界，再按主键分批删除。
        """
        deleted = {"tdx_log": 0, "trade_log": 0}
        now = datetime.now()
        tdx_cutoff = (now - timedelta(days=tdx_retention_days)).isoformat(
            timespec="milliseconds")
        trade_cutoff = (now - timedelta(days=trade_retention_days)).isoformat(
            timespec="milliseconds")

        deleted["tdx_log"] += self._prune_by_time(
            "tdx_log", tdx_cutoff, batch_size, sleep_s)
        # 行数上限：找出第 max_rows+1 新的那一行，删除它及更旧的
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tdx_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                (tdx_max_rows,)).fetchone()
        if row is not None:
            deleted["tdx_log"] += self._delete_upto_id(
                "tdx_log", row[0], batch_size, sleep_s)

        deleted["trade_log"] += self._prune_by_time(
            "trade_log", trade_cutoff, batch_size, sleep_s)

        with self._lock:
            try:
                self._conn.execute("PRAGMA optimize")
                self._conn.commit()
            except sqlite3.Error:
                log.warning("PRAGMA optimize 失败(忽略)", exc_info=True)
        # 回收空闲页：新库在建表前已设 auto_vacuum=INCREMENTAL，无需整库 VACUUM。
        # 旧库（auto_vacuum=0）只会白白执行，不做全量重写，零风险。
        with self._lock:
            try:
                self._conn.execute("PRAGMA incremental_vacuum(4000)")
                self._conn.commit()
            except sqlite3.Error:
                log.warning("incremental_vacuum 失败(忽略)", exc_info=True)
        return deleted

    def _prune_by_time(self, table: str, cutoff: str, batch_size: int,
                       sleep_s: float) -> int:
        """删除 created_at 早于 cutoff 的行；返回删除行数。"""
        self._check_log_table(table)
        with self._lock:
            row = self._conn.execute(
                f"SELECT MIN(id) FROM {table} WHERE created_at >= ?", (cutoff,)
            ).fetchone()
        boundary = row[0] if row else None
        if boundary is None:
            # 没有一行新于 cutoff ⇒ 全部已过期
            return self._delete_upto_id(table, None, batch_size, sleep_s)
        if boundary <= 1:
            return 0
        return self._delete_upto_id(table, boundary - 1, batch_size, sleep_s)

    def _delete_upto_id(self, table: str, max_id, batch_size: int,
                        sleep_s: float) -> int:
        """按主键分批删除 id <= max_id 的行；max_id=None 表示全表。"""
        self._check_log_table(table)
        total = 0
        while True:
            with self._lock:
                cur = self._conn.cursor()
                if max_id is None:
                    cur.execute(
                        f"DELETE FROM {table} WHERE id IN "
                        f"(SELECT id FROM {table} ORDER BY id LIMIT ?)",
                        (batch_size,))
                else:
                    cur.execute(
                        f"DELETE FROM {table} WHERE id IN "
                        f"(SELECT id FROM {table} WHERE id <= ? ORDER BY id LIMIT ?)",
                        (max_id, batch_size))
                n = cur.rowcount
                self._conn.commit()
            total += max(n, 0)
            if n < batch_size:
                break
            if sleep_s:
                time.sleep(sleep_s)
        return total

    @staticmethod
    def _check_log_table(table: str) -> None:
        """表名白名单，防 SQL 注入（本模块唯一拼接表名的地方）。"""
        if table not in ("tdx_log", "trade_log"):
            raise ValueError(f"不允许清理表: {table}")

    def vacuum(self) -> None:
        """回收已删除页的磁盘空间。**需要独占锁，必须在桥停止后执行。**"""
        with self._lock:
            self._conn.execute("VACUUM")

    # ---- Token 管理 (SHA256 哈希存储, 不存明文) ----

    @staticmethod
    def _hash_token(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def add_token(self, token: str, name: str = "") -> bool:
        """新增 token (返回 False 表示已存在)."""
        h = self._hash_token(token)
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM tokens WHERE token_hash=?", (h,)).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO tokens (token_hash, name, active, created_at) VALUES (?,?,?,?)",
                (h, name, 1, datetime.now().isoformat(timespec="seconds")))
            self._conn.commit()
            return True

    def delete_token(self, token_hash: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tokens WHERE token_hash=?", (token_hash,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_tokens(self) -> list[dict]:
        return self._query(
            "SELECT id, token_hash, name, active, created_at FROM tokens "
            "ORDER BY id DESC")

    def token_hashes(self) -> set:
        """返回全部活跃 token 的哈希集合."""
        rows = self._query("SELECT token_hash FROM tokens WHERE active=1")
        return {r["token_hash"] for r in rows}

    def is_token_valid(self, token: str) -> bool:
        """校验 token 是否有效."""
        h = self._hash_token(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tokens WHERE token_hash=? AND active=1", (h,)).fetchone()
            return bool(row)
