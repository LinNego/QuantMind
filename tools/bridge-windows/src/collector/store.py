"""1m bars 持久化：独立 SQLite WAL 库（不碰桥的 tdx_cache.db）。"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

_COLUMNS = ("trade_date", "code", "minute", "open", "high", "low",
            "close", "volume", "amount", "samples", "source")


class MarketStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create()

    def _create(self) -> None:
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bars_1m (
                trade_date TEXT NOT NULL,
                code       TEXT NOT NULL,
                minute     TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL,
                samples INTEGER,
                source TEXT,
                PRIMARY KEY (trade_date, code, minute)
            );
            CREATE INDEX IF NOT EXISTS idx_bars_1m_code_date
                ON bars_1m(code, trade_date);
            CREATE INDEX IF NOT EXISTS idx_bars_1m_date
                ON bars_1m(trade_date);
            CREATE TABLE IF NOT EXISTS universe (
                code TEXT PRIMARY KEY,
                updated_at TEXT
            );
            """)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- bars ----

    def upsert_bars(self, bars: list[dict]) -> int:
        if not bars:
            return 0
        rows = [tuple(bar.get(c) for c in _COLUMNS) for bar in bars]
        with self._lock:
            self._conn.executemany("""
                INSERT INTO bars_1m
                    (trade_date, code, minute, open, high, low, close,
                     volume, amount, samples, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, code, minute) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    amount=excluded.amount, samples=excluded.samples,
                    source=excluded.source
                WHERE excluded.samples >= bars_1m.samples
            """, rows)
            self._conn.commit()
        return len(rows)

    def query_bars(self, codes: list[str] | None = None,
                   start_date: str = "", end_date: str = "",
                   limit: int = 5000) -> list[dict]:
        limit = max(1, min(int(limit), 20000))
        base = ("SELECT trade_date, code, minute, open, high, low, close, "
                "volume, amount, samples, source FROM bars_1m WHERE 1=1")
        params: list = []
        if codes:
            base += f" AND code IN ({','.join('?' for _ in codes)})"
            params.extend(codes)
        if start_date:
            base += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            base += " AND trade_date <= ?"
            params.append(end_date)
        # M6：先倒序取“最近 N 条”，再按时间正序返回；无日期区间也不会拿最旧数据
        sql = ("SELECT * FROM (" + base +
               " ORDER BY trade_date DESC, minute DESC, code DESC LIMIT ?) "
               "ORDER BY trade_date, minute, code")
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    # ---- universe ----

    def save_universe(self, codes: list[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("DELETE FROM universe")
            self._conn.executemany(
                "INSERT OR REPLACE INTO universe(code, updated_at) VALUES (?,?)",
                [(c, now) for c in codes])
            self._conn.commit()

    def load_universe(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT code FROM universe ORDER BY code").fetchall()
        return [r[0] for r in rows]

    # ---- 维护 ----

    def prune(self, retention_days: int) -> int:
        cutoff = (date.today() - timedelta(days=max(retention_days, 1))).strftime("%Y%m%d")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bars_1m WHERE trade_date < ?", (cutoff,))
            deleted = max(cur.rowcount, 0)
            self._conn.commit()
            try:
                self._conn.execute("PRAGMA optimize")
                self._conn.commit()
            except sqlite3.Error:
                log.warning("PRAGMA optimize 失败(忽略)", exc_info=True)
        return deleted

    def stats(self) -> dict:
        """轻量统计：独立只读连接、不持写锁；行数用 MAX(rowid) 近似。

        90 天 × 5,500 × 240 ≈ 1.18 亿行，COUNT(*) 会数秒到数十秒并阻塞
        upsert_bars（桥侧 COUNT 事故在采集器复刻）。这里只取 O(1) 的
        MAX(rowid) 与索引端点，精确行数不再对 /health 重要。
        """
        row = (0, None, None)
        universe = 0
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cur = conn.cursor()
            row = cur.execute(
                "SELECT MAX(rowid), MIN(trade_date), MAX(trade_date) FROM bars_1m"
            ).fetchone() or (0, None, None)
            universe = cur.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()
        try:
            size = os.path.getsize(self.db_path)
        except OSError:
            size = 0
        free = 0
        try:
            import shutil
            free = shutil.disk_usage(os.path.dirname(self.db_path) or ".").free
        except OSError:
            pass
        return {
            "rows": row[0] or 0,
            "rows_approx": True,
            "min_date": row[1],
            "max_date": row[2],
            "universe": universe,
            "disk_bytes": size,
            "disk_free_bytes": free,
        }
