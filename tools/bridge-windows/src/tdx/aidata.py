"""TdxAiData（通达信独立数据接口 tqs）封装。

与 TdxW 内置通道的区别
----------------------
- 本模块走 `tdxaidata` 包（`from tdxaidata import tqs`），**不需要启动通达信客户端**，
  DLL 直连通达信数据服务；
- 需要**付费 Key**（`TdxAiData.ini [Token] token=`，商城→我的订单→积分和Key管理）；
- 能取 TQ 客户端取不到的东西：逐笔成交（含 B/S）、历史分时、竞价、批量快照、
  实时推送（完整盘口 JSON）、公式源码计算（1.3.0+）。

设计约束
--------
- 所有 tqs 调用都是阻塞 DLL 调用，**必须**在专用线程池里执行（调用方负责）；
- tqs 是进程级单例，内部有全局回调表，这里用一把锁串行化；
- 包可能没装 / Token 可能没配 —— 一律返回结构化状态，不在 import 期炸。
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger(__name__)

# 只允许读写快照类方法；订阅/公式等由专门入口处理。
READONLY_METHODS = frozenset({
    "get_minute_data", "get_tick_data", "get_market_snapshot",
    "get_market_snapshot_batch", "get_market_data", "get_global_data",
    "get_zzgz_stocklist", "get_call_auction", "get_call_auction_batch",
    "formula_by_src", "formula_by_file", "get_stock_mark",
    "get_exday_data", "get_pricevol", "get_stock_list",
    "get_stock_list_in_sector", "get_more_info", "get_more_info_batch",
    "get_stock_info", "get_stock_info_batch", "get_domain_sorthq",
    "get_subscribe_hq_stock_list", "get_gpjy_value", "get_bkjy_value",
    "get_scjy_value", "get_gp_one_data",
    # 2026-10-23 付费 Key 打通后补：包里有、白名单之前没放行的只读方法
    "get_trading_dates", "get_financial_data", "get_financial_data_by_date",
    "get_gpjy_value_by_date", "get_bkjy_value_by_date", "get_scjy_value_by_date",
    "get_divid_factors", "get_gb_info", "get_gb_info_by_date",
    "get_kzz_info", "get_kzz_info_batch", "get_ipo_info",
    "get_sector_list", "get_sector_list_ds", "get_user_sector",
    "get_relation", "get_trackzs_etf_info", "get_zdt_data",
    "get_block_from_stock", "get_match_stkinfo",
})

_TOKEN_HINT = "Need TokenKey"


class AidataUnavailable(RuntimeError):
    """包/DLL 不可用，或方法不存在。"""


class AidataTokenRequired(RuntimeError):
    """未配置付费 Key，或 Key 无效。"""


class AidataService:
    """tdxaidata 的门面：状态、Token 管理、只读查询、订阅。"""

    def __init__(self, vendor_dir: str = "", ini_path: str = "",
                 enabled: bool = True, usage_path: str = "",
                 max_rows_per_call: int = 0,
                 tokens_per_call: float = 1.0):
        self.enabled = enabled
        self.vendor_dir = vendor_dir or ""
        self._ini_override = ini_path or ""
        self.usage_path = usage_path or ""
        # 实测（2026-10-23 商城明细）：**1 词元/次**，与返回行数无关。
        # max_rows_per_call 只作安全阀（防一次拉几十万行卡死），0=不限制。
        self.max_rows_per_call = max(0, int(max_rows_per_call or 0))
        self.tokens_per_call = float(tokens_per_call or 1.0)
        self._usage_lock = threading.Lock()
        self._usage = self._load_usage()
        self._lock = threading.Lock()
        self._tqs = None
        self._pkg_dir = ""
        self._version = ""
        self._load_error = ""
        self._last_error = ""
        if enabled:
            self._load()

    # ---- 加载 ----

    def _load(self) -> None:
        try:
            if self.vendor_dir and self.vendor_dir not in sys.path:
                sys.path.insert(0, self.vendor_dir)
            import tdxaidata  # noqa: PLC0415
            from tdxaidata import tqs  # noqa: PLC0415

            self._tqs = tqs
            self._pkg_dir = os.path.dirname(os.path.abspath(tdxaidata.__file__))
            self._version = str(getattr(tdxaidata, "__version__", "") or "")
            log.info("TdxAiData 已加载: v%s @ %s", self._version, self._pkg_dir)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {exc}"
            log.warning("TdxAiData 不可用: %s", self._load_error)

    @property
    def ini_path(self) -> str:
        if self._ini_override:
            return self._ini_override
        if self._pkg_dir:
            return os.path.join(self._pkg_dir, "lib", "TdxAiData.ini")
        return ""

    def methods(self) -> list[str]:
        if self._tqs is None:
            return []
        return sorted(n for n in dir(self._tqs) if not n.startswith("_"))

    # ---- Token ----

    def read_token(self) -> str:
        path = self.ini_path
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            return ""
        # 注意：不要用 \s —— 多行模式下 \s* 会跨行，把下一节的 "[TcHqHost]" 当成 token。
        m = re.search(r"(?mi)^[ \t]*token[ \t]*=[ \t]*([^\s]*)[ \t]*$", text)
        return m.group(1) if m else ""

    def set_token(self, token: str) -> dict:
        """写入 TdxAiData.ini（保留其余内容与注释）。空串表示清除。"""
        token = (token or "").strip()
        if token and (len(token) < 16 or len(token) > 256 or re.search(r"\s", token)):
            raise ValueError("token 不合法（16-256 个非空白字符）")
        path = self.ini_path
        if not path:
            raise AidataUnavailable("TdxAiData 未加载，无法定位 TdxAiData.ini")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        else:
            text = "[Token]\ntoken=\n"
        if re.search(r"(?mi)^[ \t]*token[ \t]*=", text):
            text = re.sub(r"(?mi)^([ \t]*token[ \t]*=).*$",
                          lambda m: m.group(1) + token, text, count=1)
        elif re.search(r"(?mi)^[ \t]*\[Token\][ \t]*$", text):
            text = re.sub(r"(?mi)^([ \t]*\[Token\][ \t]*)$",
                          lambda m: f"{m.group(1)}\ntoken={token}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n\n[Token]\ntoken={token}\n"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self._last_error = ""
        log.info("TdxAiData token 已更新（len=%d）", len(token))
        return {"ini_path": path, "token_configured": bool(token),
                "token_len": len(token)}

    # ---- 用量台账（按天/按方法 + 余额校准） ----

    def _empty_usage(self) -> dict:
        return {"days": {}, "totals": {"calls": 0, "bytes": 0, "rows": 0},
                "balance": None}

    def _load_usage(self) -> dict:
        if not self.usage_path or not os.path.exists(self.usage_path):
            return self._empty_usage()
        try:
            with open(self.usage_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return self._empty_usage()
        if not isinstance(data, dict):
            return self._empty_usage()
        data.setdefault("days", {})
        data.setdefault("totals", {"calls": 0, "bytes": 0, "rows": 0})
        data.setdefault("balance", None)
        return data

    def _today(self) -> dict:
        today = datetime.now().strftime("%Y%m%d")
        day = self._usage["days"].setdefault(
            today, {"calls": 0, "bytes": 0, "rows": 0, "by_method": {}})
        day.setdefault("by_method", {})
        # 只保留最近 14 天
        keys = sorted(self._usage["days"])[-14:]
        self._usage["days"] = {k: self._usage["days"][k] for k in keys}
        return day

    def _persist_usage(self) -> None:
        if not self.usage_path:
            return
        os.makedirs(os.path.dirname(self.usage_path) or ".", exist_ok=True)
        tmp = self.usage_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._usage, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.usage_path)
        except OSError as exc:
            log.warning("aidata 用量落盘失败(忽略): %s", exc)

    @staticmethod
    def _count_rows(result: Any) -> int:
        """估算返回的“数据行”数：分时/逐笔=数组长度；批量快照=证券只数。"""
        if isinstance(result, dict):
            lists = [len(v) for v in result.values() if isinstance(v, list)]
            if lists:
                return max(lists)
            if result and all(isinstance(v, dict) for v in result.values()):
                return len(result)  # 批量快照：{code: {...}}
            return 1
        if isinstance(result, list):
            return len(result)
        return 1

    def _record_usage(self, method: str, result: Any) -> None:
        try:
            size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            size = 0
        rows = self._count_rows(result)
        with self._usage_lock:
            day = self._today()
            for target in (day, day["by_method"].setdefault(
                    method, {"calls": 0, "bytes": 0, "rows": 0}),
                    self._usage["totals"]):
                target["calls"] += 1
                target["bytes"] += size
                target["rows"] += rows
            self._persist_usage()

    def record_balance(self, value: int) -> dict:
        """人工记一次商城余额（词元）。之后即可用真实差值校准单价。"""
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("余额必须是整数") from None
        if value < 0:
            raise ValueError("余额不能为负")
        with self._usage_lock:
            totals = self._usage["totals"]
            self._usage["balance"] = {
                "value": value,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "calls": totals["calls"],
                "rows": totals["rows"],
                "bytes": totals["bytes"],
            }
            self._persist_usage()
        return self._usage["balance"]

    def usage_status(self) -> dict:
        with self._usage_lock:
            day = self._today()
            data = json.loads(json.dumps(self._usage, ensure_ascii=False))
        balance = data.get("balance") or None
        calibration = None
        if balance:
            d_calls = data["totals"]["calls"] - int(balance.get("calls", 0))
            d_rows = data["totals"]["rows"] - int(balance.get("rows", 0))
            if d_rows > 0 or d_calls > 0:
                calibration = {"since_calls": d_calls, "since_rows": d_rows}
        today_key = datetime.now().strftime("%Y%m%d")
        return {
            "day": today_key,
            "today": data["days"].get(today_key, day),
            "totals": data["totals"],
            "days": data["days"],
            "balance": balance,
            "calibration": calibration,
            "est_tokens_today": round(day["calls"] * self.tokens_per_call, 1),
            "tokens_per_call": self.tokens_per_call,
        }

    def _check_row_budget(self, params: dict) -> None:
        """安全阀：单次返回行数上限（按次计费，行数不额外收费；0=不限）。"""
        if self.max_rows_per_call <= 0:
            return
        for key in ("wantnum", "count", "limit", "stock_page_size"):
            raw = params.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if key == "wantnum" and value <= 0:
                continue  # 0=不限，交给下面的默认上限
            if key == "wantnum" and value == 0:
                continue
            if value > self.max_rows_per_call:
                raise ValueError(
                    f"{key}={value} 超过单次上限 {self.max_rows_per_call}"
                    f"（aidata.max_rows_per_call 可调）")

    # ---- 状态 ----

    def status(self) -> dict:
        token = self.read_token()
        subscribed: list[str] = []
        if self._tqs is not None:
            try:
                subscribed = list(self._tqs.get_subscribe_hq_stock_list() or [])
            except Exception:  # noqa: BLE001
                subscribed = []
        return {
            "enabled": self.enabled,
            "available": self._tqs is not None,
            "version": self._version,
            "package_dir": self._pkg_dir,
            "ini_path": self.ini_path,
            "ini_exists": bool(self.ini_path) and os.path.exists(self.ini_path),
            "token_configured": bool(token),
            "token_len": len(token),
            "methods": self.methods(),
            "readonly_methods": sorted(READONLY_METHODS),
            "subscribed": subscribed,
            "load_error": self._load_error,
            "last_error": self._last_error,
            "vendor_dir": self.vendor_dir,
            "max_rows_per_call": self.max_rows_per_call,
            "usage": self.usage_status(),
        }

    # ---- 调用 ----

    def _ensure(self):
        if self._tqs is None:
            raise AidataUnavailable(self._load_error or "tdxaidata 未加载")

    @staticmethod
    def _check_result(result: Any) -> Any:
        """tqs 多数错误是打印而非抛异常；识别 Token 缺失并转成明确异常。"""
        text = result if isinstance(result, str) else ""
        if _TOKEN_HINT in text or '"ErrorId":13' in text or '"ErrorId": 13' in text:
            raise AidataTokenRequired("TdxAiData 需要付费 Key（TdxAiData.ini [Token]）")
        return result

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        """只读方法直通。method 必须在白名单且当前版本存在。

        tqs 的失败方式是**打印**而不是抛异常（例如 ``[错误] TokenKey Insufficient``），
        返回 None。这里把调用期间的 stdout 抓下来，转成明确的 HTTP 错误，
        否则网页上只会看到 ``result: null``，用户无法判断是 Key 无效还是没数据。
        """
        if method not in READONLY_METHODS:
            raise ValueError(f"方法不在白名单: {method}")
        self._ensure()
        fn = getattr(self._tqs, method, None)
        if fn is None:
            raise AidataUnavailable(f"tdxaidata v{self._version} 无 {method}")
        self._check_row_budget(params or {})
        buffer = io.StringIO()
        try:
            with self._lock, contextlib.redirect_stdout(buffer):
                result = fn(**(params or {}))
        except AidataTokenRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{method}: {type(exc).__name__}: {exc}"
            raise AidataUnavailable(self._last_error) from exc
        captured = buffer.getvalue().strip()
        if captured:
            self._last_error = f"{method}: {captured[:300]}"
            log.warning("tdxaidata %s 输出: %s", method, captured[:300])
        if result is None and captured:
            low = captured.lower()
            if ("tokenkey" in low or "key error" in low
                    or "insufficient" in low or "token" in low and "错误" in captured):
                raise AidataTokenRequired(f"TdxAiData Key 被拒绝：{captured[:200]}")
            raise AidataUnavailable(f"tdxaidata {method} 失败：{captured[:200]}")
        checked = self._check_result(result)
        if checked is not None:
            self._record_usage(method, checked)
        return checked

    def subscribe(self, codes: list[str], callback) -> None:
        self._ensure()
        fn = getattr(self._tqs, "subscribe", None)
        if fn is None:
            raise AidataUnavailable("tdxaidata 无 subscribe")
        with self._lock:
            fn(codes, callback)

    def unsubscribe(self, codes: list[str]) -> None:
        self._ensure()
        fn = getattr(self._tqs, "unsubscribe", None)
        if fn is None:
            return
        with self._lock:
            fn(codes)
