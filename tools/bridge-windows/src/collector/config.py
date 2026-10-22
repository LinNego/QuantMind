"""采集器配置：bridge config.yaml + 环境变量，缺关键项即 fail-fast。"""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

VERSION = "1.0.0"


def _resolve(value):
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 不是合法数字: {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 不是合法整数: {raw!r}") from exc


@dataclass(frozen=True)
class CollectorConfig:
    bridge_url: str
    bridge_token: str
    host: str
    port: int
    token: str
    db_path: str
    log_dir: str
    snapshot_interval: float
    hot_interval: float
    snapshot_chunk: int
    hot_set: tuple[str, ...]
    retention_days: int
    slow_call_ms: float
    error_pause_s: float
    version: str = VERSION


def load_config(config_path: str, root_dir: str) -> CollectorConfig:
    """读取桥的 config.yaml + 采集器环境变量。

    token 规则：`COLLECTOR_TOKEN` 优先；否则复用桥的 `auth.token`（当前同钥匙，
    Phase 2 再拆只读 token）。两者都没有 → ValueError，绝不空 token 起服务。
    """
    raw: dict = {}
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    bridge_token = os.environ.get("BRIDGE_TOKEN") or _resolve(
        (raw.get("auth") or {}).get("token", ""))
    if not bridge_token:
        raise ValueError("桥 token 未配置：设置 BRIDGE_TOKEN 或 config.yaml auth.token")

    token = os.environ.get("COLLECTOR_TOKEN") or bridge_token

    http_cfg = (raw.get("channels") or {}).get("http") or {}
    bridge_port = int(http_cfg.get("port", 8550))
    bridge_url = os.environ.get("BRIDGE_URL") or f"http://127.0.0.1:{bridge_port}"

    hot_raw = os.environ.get("COLLECTOR_HOT_SET", "")
    hot_set = tuple(x.strip().upper() for x in hot_raw.split(",") if x.strip())

    snapshot_interval = _env_float("COLLECTOR_SNAPSHOT_INTERVAL", 5.0)
    hot_interval = _env_float("COLLECTOR_HOT_INTERVAL", 3.0)
    snapshot_chunk = _env_int("COLLECTOR_SNAPSHOT_CHUNK", 500)
    if not (1.0 <= snapshot_interval <= 60.0):
        raise ValueError("COLLECTOR_SNAPSHOT_INTERVAL 必须在 1–60 秒")
    if not (0.5 <= hot_interval <= 30.0):
        raise ValueError("COLLECTOR_HOT_INTERVAL 必须在 0.5–30 秒")
    if not (50 <= snapshot_chunk <= 2000):
        raise ValueError("COLLECTOR_SNAPSHOT_CHUNK 必须在 50–2000")

    return CollectorConfig(
        bridge_url=bridge_url.rstrip("/"),
        bridge_token=bridge_token,
        host=os.environ.get("COLLECTOR_HOST", "0.0.0.0"),
        port=_env_int("COLLECTOR_PORT", 8560),
        token=token,
        db_path=os.environ.get("COLLECTOR_DB")
        or os.path.join(root_dir, "data", "market_1m.db"),
        log_dir=os.environ.get("COLLECTOR_LOG_DIR")
        or os.path.join(root_dir, "data", "logs"),
        snapshot_interval=snapshot_interval,
        hot_interval=hot_interval,
        snapshot_chunk=snapshot_chunk,
        hot_set=hot_set,
        retention_days=_env_int("COLLECTOR_RETENTION_DAYS", 90),
        slow_call_ms=_env_float("COLLECTOR_SLOW_CALL_MS", 3000.0),
        error_pause_s=_env_float("COLLECTOR_ERROR_PAUSE_S", 30.0),
    )
