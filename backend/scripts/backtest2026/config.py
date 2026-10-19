"""2026 TopK 回测的冻结口径与路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw) if raw else default


def _default_data_volume() -> Path:
    """宿主用仓库 ``data/``，容器内用挂载点 ``/data``。"""
    container_volume = Path("/data")
    repo_volume = REPO_ROOT / "data"
    if (container_volume / "quantdb").is_dir() and not (
        repo_volume / "quantdb"
    ).is_dir():
        return container_volume
    return _env_path("QM_DATA_ROOT", repo_volume)


DATA_VOLUME = _default_data_volume()
DATA_ROOT = _env_path("QM_QUANTDB_DATA_DIR", DATA_VOLUME / "quantdb")
OUT_DIR = _env_path("QM_BACKTEST2026_OUT", DATA_VOLUME / "backtest_2026")

T5_RUN_DIR = DATA_VOLUME / "training_jobs" / "train_20261018034514_7e9b6d1d"
T10_RUN_DIR = DATA_VOLUME / "training_jobs" / "train_20261018162000_182d5dcf"

#: 回测窗口：2026 年首个交易日至预测/特征允许的最后外推日。
SIGNAL_START = "2026-01-05"
STORED_PRED_END = "2026-08-27"
EXTEND_START = "2026-08-28"
EXTEND_END = "2026-09-14"

#: 价格面板向前多取一个月，保证 2026-01-05 有昨收；向后覆盖 T+10 退出。
PRICE_START = "2025-12-01"
PRICE_END = "2026-10-16"

#: 静态 ST 名单的 as-of 分区（QuantDB 无更早的逐日 ST）。
ST_ASOF_DT = "20261016"

INITIAL_CAPITAL = 2_000_000.0
LOT_SIZE = 100
TOP20_K = 20
TOP20_PER_NAME = 100_000.0
ROLLING_K = 5
ROLLING_DAYS = 5

COMMISSION_RATE = 0.0003  # 用户口径：双边万 3
STAMP_DUTY_RATE = 0.0005  # 敏感性：卖出印花税
SLIPPAGE = 0.0  # 主结果不计滑点；敏感性单列
MAX_EXIT_DEFER_DAYS = 10
MIN_LISTING_DAYS = 60

PRICE_EPS_YUAN = 0.004  # 与 market_breadth.price_tolerance 同源
ST_LIMIT_RELAXED_FROM = "2026-07-06"

INDEX_SYMBOLS = ("000300.SH", "000905.SH", "000852.SH")


@dataclass(frozen=True)
class ModelSpec:
    name: str
    horizon: int
    run_dir: Path


MODELS = (
    ModelSpec("T5", 5, T5_RUN_DIR),
    ModelSpec("T10", 10, T10_RUN_DIR),
)
