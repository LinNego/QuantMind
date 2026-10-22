"""期货面板装载与主力连续复权。

数据来源（QuantFutures canonical 目录）：

* ``1_kline_data/daily_forward``：主力连续日线（一行一个品种一天）
* ``2_base_sector/contracts_daily``：逐合约日线 + 持仓量（2022-09 起）
* ``2_base_sector/spot_basis``：现货基差（2024-11 起）
* ``2_base_sector/inventory_em`` / ``warehouse_receipts``：库存与仓单
* ``2_base_sector/member_rank_sina``：交易所会员持仓排名
* ``2_base_sector/contract_meta``：近期合约的准确到期日（用于覆盖估算）

主力连续在换月日会出现价格跳空。复权口径：

    adj_close_t = adj_close_{t-1} * close(D_t, t) / close(D_t, t-1)

其中 ``D_t`` 是 t 日持仓量最大的合约。最后一日锚定到原始主力价，
因此复权序列的水平与当前盘面一致；复权只改变历史相对水平，不改变收益。

2022-09 之前没有逐合约数据，无法做移仓复权：这段历史用原始主力连续价，
再乘以 2022-09 首日的复权乘数拼接，保证跨边界收益不出现假跳。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.futures_factors.expiry import (
    ContractCode,
    TradingCalendar,
    estimate_last_trade_date,
    parse_contract_code,
)
from backend.shared.futures_universe import symbol_meta

__all__ = [
    "DataPaths",
    "attach_dominant_contract",
    "attach_last_trade_dates",
    "build_roll_adjusted_prices",
    "calendar_from_main",
    "combine_contract_panels",
    "expiry_overrides_from_meta",
    "load_contract_meta",
    "load_contracts",
    "load_inventory",
    "load_main_continuous",
    "load_member_positions",
    "load_spot_basis",
    "load_warehouse",
    "normalize_contract_panel",
    "normalize_main_continuous",
    "parse_contract_symbol",
    "variety_of_main_symbol",
]

MAIN_REQUIRED = ("symbol", "time", "open", "high", "low", "close", "volume", "amount")
CONTRACT_REQUIRED = ("symbol", "time", "close", "open_interest")
MAIN_SUFFIX = "0.CN"


@dataclass(frozen=True)
class DataPaths:
    """QuantFutures 数据根目录与各数据集路径。"""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    @classmethod
    def from_env(cls, root: str | Path | None = None) -> DataPaths:
        if root is None:
            root = os.environ.get("QM_QUANTFUTURES_DATA_DIR", "data/quantfutures")
        return cls(Path(root))

    @property
    def main_continuous(self) -> Path:
        return self.root / "1_kline_data" / "daily_forward"

    @property
    def contracts_daily(self) -> Path:
        return self.root / "2_base_sector" / "contracts_daily"

    @property
    def spot_basis(self) -> Path:
        return self.root / "2_base_sector" / "spot_basis"

    @property
    def inventory(self) -> Path:
        return self.root / "2_base_sector" / "inventory_em"

    @property
    def warehouse(self) -> Path:
        return self.root / "2_base_sector" / "warehouse_receipts"

    @property
    def member_positions(self) -> Path:
        return self.root / "2_base_sector" / "member_rank_sina"

    @property
    def contract_meta(self) -> Path:
        return self.root / "2_base_sector" / "contract_meta"


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def variety_of_main_symbol(symbol: str) -> str:
    """``CU0.CN`` -> ``CU``。"""
    stem = str(symbol).rsplit(".", 1)[0]
    if not stem.endswith("0"):
        raise ValueError(f"不是主力连续代码: {symbol!r}")
    return stem[:-1].upper()


def parse_contract_symbol(symbol: str, ref_date=None) -> ContractCode:
    return parse_contract_code(symbol, ref_date=ref_date)


def _require_columns(df: pd.DataFrame, required: Sequence[str], name: str) -> None:
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{name} 缺少列: {', '.join(missing)}")


_STALE_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def _drop_stale_rows(df: pd.DataFrame) -> pd.DataFrame:
    """剔除整行与前一交易日完全相同的记录（数据源的假期填充）。

    真实交易中 OHLCV 与成交额逐位复制的概率可忽略；这些行会让 1~5 日
    标签在假交易日上多出 0 收益样本，并压低波动率。逐位比较不误伤
    涨跌停（成交量/成交额不会与前一交易日一模一样）。
    """
    previous = df.groupby("symbol", sort=False)[list(_STALE_COLUMNS)].shift(1)
    same = (df[list(_STALE_COLUMNS)] == previous).all(axis=1)
    same &= previous["close"].notna()
    return df[~same]


def _repair_ohlc_bounds(df: pd.DataFrame) -> pd.DataFrame:
    """把越界的最高/最低价收敛到 [min(open, close), max(open, close)]。

    只修正非空值；缺失保持缺失——把缺失高点填成收盘价会伪造零振幅。
    """
    high_valid = df["high"].notna()
    df.loc[high_valid, "high"] = df.loc[high_valid, ["high", "open", "close"]].max(
        axis=1
    )
    low_valid = df["low"].notna()
    df.loc[low_valid, "low"] = df.loc[low_valid, ["low", "open", "close"]].min(axis=1)
    return df


def normalize_main_continuous(df: pd.DataFrame) -> pd.DataFrame:
    """过滤境内主力连续、统一类型、去重并按品种/日期排序。

    额外做两项源数据清洗（见 ``_drop_stale_rows`` / ``_repair_ohlc_bounds``）：
    假期填充行必须剔除，否则短周期标签会被假交易日污染。
    """
    _require_columns(df, MAIN_REQUIRED, "daily_forward")
    out = df.copy()
    out["symbol"] = out["symbol"].astype(str)
    out = out[out["symbol"].str.endswith(MAIN_SUFFIX)].copy()
    if out.empty:
        raise ValueError("daily_forward 中没有境内主力连续（*0.CN）数据")
    out["variety"] = out["symbol"].map(variety_of_main_symbol)
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    out = out.drop_duplicates(["symbol", "time"], keep="last")
    out = out.sort_values(["symbol", "time"]).reset_index(drop=True)
    out = _drop_stale_rows(out)
    out = _repair_ohlc_bounds(out)
    return out.sort_values(["variety", "time"]).reset_index(drop=True)


def normalize_contract_panel(df: pd.DataFrame, ref_date=None) -> pd.DataFrame:
    """补品种/交割年月，清洗价格与持仓量。"""
    _require_columns(df, CONTRACT_REQUIRED, "contracts_daily")
    out = df.copy()
    out["symbol"] = out["symbol"].astype(str)
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["open_interest"] = pd.to_numeric(out["open_interest"], errors="coerce")
    out = out.dropna(subset=["close", "open_interest"])
    out = out[(out["close"] > 0) & (out["open_interest"] >= 0)]
    codes = [parse_contract_symbol(sym, ref_date=ref_date) for sym in out["symbol"]]
    out["variety"] = [c.variety for c in codes]
    out["contract_year"] = [c.year for c in codes]
    out["contract_month"] = [c.month for c in codes]
    if "last_trade_date" not in out.columns:
        out["last_trade_date"] = pd.NaT
    out["last_trade_date"] = pd.to_datetime(out["last_trade_date"], errors="coerce")
    out = out.drop_duplicates(["symbol", "time"], keep="last")
    return out.sort_values(["variety", "time", "symbol"]).reset_index(drop=True)


def combine_contract_panels(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=list(CONTRACT_REQUIRED) + ["variety"])
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["symbol", "time"], keep="last")
    return out.sort_values(["variety", "time", "symbol"]).reset_index(drop=True)


def calendar_from_main(main: pd.DataFrame) -> TradingCalendar:
    return TradingCalendar.from_dates(pd.to_datetime(main["time"]).unique())


# ---------------------------------------------------------------------------
# 到期日与主力合约
# ---------------------------------------------------------------------------


def attach_last_trade_dates(
    contracts: pd.DataFrame,
    calendar: TradingCalendar,
    overrides: dict[tuple[str, int, int], object] | None = None,
) -> pd.DataFrame:
    """给每个合约补最后交易日：优先交易所公布值，否则按规则估算。"""
    out = contracts.copy()
    if out.empty:
        return out
    need = out["last_trade_date"].isna()
    if not need.any():
        return out
    # 同一 (品种, 年月) 只算一次
    keys = (
        out.loc[need, ["variety", "contract_year", "contract_month"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )
    lookup: dict[tuple[str, int, int], object] = {}
    for variety, year, month in keys:
        exchange = symbol_meta(f"{variety}0.CN").get("exchange", "")
        lookup[(variety, int(year), int(month))] = estimate_last_trade_date(
            variety,
            int(year),
            int(month),
            calendar,
            exchange=exchange,
            overrides=overrides,
        )
    mask = out["last_trade_date"].isna()
    mapped = [
        lookup.get((v, int(y), int(m)))
        for v, y, m in out.loc[
            mask,
            [
                "variety",
                "contract_year",
                "contract_month",
            ],
        ].itertuples(index=False, name=None)
    ]
    out.loc[mask, "last_trade_date"] = pd.to_datetime(
        pd.Series(mapped), errors="coerce"
    )
    return out


def attach_dominant_contract(contracts: pd.DataFrame) -> pd.DataFrame:
    """标记每个品种每日持仓量最大的合约（并列时取更近到期）。"""
    if contracts.empty:
        out = contracts.copy()
        out["is_dominant"] = pd.Series(dtype=bool)
        return out
    work = contracts.copy()
    if "last_trade_date" not in work.columns:
        work["last_trade_date"] = pd.NaT
    work["last_trade_date"] = pd.to_datetime(work["last_trade_date"], errors="coerce")
    ordered = work.sort_values(
        ["variety", "time", "open_interest", "last_trade_date"],
        ascending=[True, True, True, False],
        na_position="last",
    )
    winners = ordered.drop_duplicates(["variety", "time"], keep="last")
    keys = winners[["symbol", "time"]].assign(is_dominant=True)
    work = work.merge(keys, on=["symbol", "time"], how="left")
    work["is_dominant"] = work["is_dominant"].fillna(False).astype(bool)
    return work


# ---------------------------------------------------------------------------
# 移仓复权
# ---------------------------------------------------------------------------


def _empty_adjusted(main: pd.DataFrame) -> pd.DataFrame:
    out = main.copy()
    out["roll_flag"] = 0.0
    out["meta_roll_adjusted"] = 0.0
    return out


def build_roll_adjusted_prices(
    main: pd.DataFrame, contracts: pd.DataFrame
) -> pd.DataFrame:
    """用主力合约自身收益链复权，并把 2022-09 之前的价格拼接到同一水平。"""
    if contracts is None or contracts.empty:
        return _empty_adjusted(main)

    dom = attach_dominant_contract(contracts)
    dom = dom[dom["is_dominant"]].copy()
    if dom.empty:
        return _empty_adjusted(main)

    dom = dom.sort_values(["variety", "time"]).reset_index(drop=True)
    grouped = dom.groupby("variety", sort=False)
    dom["prev_time"] = grouped["time"].shift(1)
    dom["prev_symbol"] = grouped["symbol"].shift(1)

    # 复权需要的是“当前主力合约在上一交易日的收盘价”，
    # 即 close(D_t, t-1)，而不是上一日主力合约的收盘价。
    prev = contracts[["symbol", "time", "close"]].rename(
        columns={"time": "prev_time", "close": "prev_contract_close"}
    )
    dom = dom.merge(prev, on=["symbol", "prev_time"], how="left")
    valid_prev = dom["prev_contract_close"].where(dom["prev_contract_close"] > 0)
    dom["ratio"] = (dom["close"] / valid_prev).fillna(1.0)
    dom["factor"] = dom.groupby("variety", sort=False)["ratio"].cumprod()
    dom["roll_flag"] = (
        (dom["symbol"] != dom["prev_symbol"]) & dom["prev_symbol"].notna()
    ).astype(float)

    raw = main[["variety", "time", "close"]].rename(columns={"close": "raw_close"})
    dom = dom.merge(raw, on=["variety", "time"], how="left")
    dom = dom.dropna(subset=["raw_close"])
    if dom.empty:
        return _empty_adjusted(main)

    last = dom.groupby("variety", sort=False).tail(1).set_index("variety")
    scale = (last["raw_close"] / last["factor"]).to_dict()
    dom["multiplier"] = dom["variety"].map(scale) * dom["factor"] / dom["raw_close"]

    mult = dom[["variety", "time", "multiplier", "roll_flag"]].copy()
    mult["meta_roll_adjusted"] = 1.0
    out = main.merge(mult, on=["variety", "time"], how="left")
    out = out.sort_values(["variety", "time"]).reset_index(drop=True)
    grouped = out.groupby("variety", sort=False)
    # 无合约数据的日期（主要是 2022-09 之前）沿用最近乘数，
    # 保证水平连续；这些日子的收益仍是原始主力收益。
    out["multiplier"] = grouped["multiplier"].ffill().bfill().fillna(1.0)
    out["meta_roll_adjusted"] = out["meta_roll_adjusted"].fillna(0.0)
    out["roll_flag"] = out["roll_flag"].fillna(0.0)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * out["multiplier"]
    return out.drop(columns=["multiplier"]).reset_index(drop=True)


def expiry_overrides_from_meta(
    meta: pd.DataFrame,
) -> dict[tuple[str, int, int], object]:
    """把 contract_meta 的准确到期日整理成 ``(品种, 年, 月) -> 日期``。"""
    if meta is None or meta.empty:
        return {}
    required = {"time", "symbol"}
    if not required.issubset(meta.columns):
        return {}
    out: dict[tuple[str, int, int], object] = {}
    value_cols = [c for c in ("expire_date", "last_trade_date") if c in meta.columns]
    if not value_cols:
        return {}
    work = meta.copy()
    work["time"] = pd.to_datetime(work["time"], errors="coerce")
    for row in work.itertuples(index=False):
        symbol = getattr(row, "symbol", None)
        if not isinstance(symbol, str):
            continue
        ref = getattr(row, "time", None)
        try:
            code = parse_contract_symbol(symbol, ref_date=ref)
        except ValueError:
            continue
        exact = None
        for col in value_cols:
            value = getattr(row, col, None)
            if value is not None and not pd.isna(value):
                exact = pd.Timestamp(value).date()
                break
        if exact is not None:
            out[(code.variety, code.year, code.month)] = exact
    return out


# ---------------------------------------------------------------------------
# IO（DuckDB 直读 parquet）
# ---------------------------------------------------------------------------


def _parquet_glob(path: Path, partitioned: bool) -> str:
    pattern = "*/data.parquet" if partitioned else "*.parquet"
    return str(Path(path) / pattern)


def _read_parquet(
    path: Path, columns: Sequence[str] | None, partitioned: bool
) -> pd.DataFrame:
    import duckdb

    glob = _parquet_glob(path, partitioned)
    if not any(Path(path).glob("*/data.parquet" if partitioned else "*.parquet")):
        return pd.DataFrame(columns=list(columns or []))
    select = ", ".join(columns) if columns else "*"
    con = duckdb.connect()
    try:
        return con.execute(
            f"select {select} from read_parquet(?, union_by_name=true)", [glob]
        ).df()
    finally:
        con.close()


def load_main_continuous(
    paths: DataPaths, varieties: Sequence[str] | None = None
) -> pd.DataFrame:
    df = _read_parquet(paths.main_continuous, None, partitioned=True)
    out = normalize_main_continuous(df)
    if varieties:
        wanted = {v.upper() for v in varieties}
        out = out[out["variety"].isin(wanted)]
    return out.reset_index(drop=True)


def load_contracts(
    paths: DataPaths,
    start=None,
    end=None,
    varieties: Sequence[str] | None = None,
) -> pd.DataFrame:
    cols = [
        "symbol",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "open_interest",
        "settlement",
    ]
    df = _read_parquet(paths.contracts_daily, cols, partitioned=False)
    out = normalize_contract_panel(df)
    if start is not None:
        out = out[out["time"] >= pd.Timestamp(start)]
    if end is not None:
        out = out[out["time"] <= pd.Timestamp(end)]
    if varieties:
        wanted = {v.upper() for v in varieties}
        out = out[out["variety"].isin(wanted)]
    return out.reset_index(drop=True)


def load_spot_basis(paths: DataPaths) -> pd.DataFrame:
    df = _read_parquet(paths.spot_basis, None, partitioned=True)
    if df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["variety"] = out["symbol"].astype(str).str.upper()
    for col in (
        "near_basis",
        "dom_basis",
        "near_basis_rate",
        "dom_basis_rate",
        "spot_price",
    ):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["variety", "time"]).reset_index(drop=True)


def load_inventory(paths: DataPaths) -> pd.DataFrame:
    df = _read_parquet(paths.inventory, None, partitioned=False)
    if df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["variety"] = out["symbol"].astype(str).str.upper()
    out["inventory"] = pd.to_numeric(out["inventory"], errors="coerce")
    return out.dropna(subset=["inventory"]).sort_values(["variety", "time"])


def load_warehouse(paths: DataPaths) -> pd.DataFrame:
    df = _read_parquet(paths.warehouse, None, partitioned=True)
    if df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out["variety"] = out["variety"].astype(str).str.upper()
    out["receipt_qty"] = pd.to_numeric(out["receipt_qty"], errors="coerce")
    grouped = (
        out.dropna(subset=["time", "receipt_qty"])
        .groupby(["variety", "time"], as_index=False)["receipt_qty"]
        .sum()
    )
    return grouped.sort_values(["variety", "time"])


def load_member_positions(paths: DataPaths) -> pd.DataFrame:
    df = _read_parquet(paths.member_positions, None, partitioned=True)
    if df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["variety"] = out["variety"].astype(str).str.upper()
    out["side"] = out["side"].astype(str).str.lower()
    out["size"] = pd.to_numeric(out["size"], errors="coerce")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    return out.dropna(subset=["size"]).sort_values(["variety", "time"])


def load_contract_meta(paths: DataPaths) -> pd.DataFrame:
    return _read_parquet(paths.contract_meta, None, partitioned=True)


def _numeric_series(values) -> pd.Series:
    return pd.to_numeric(pd.Series(np.asarray(values)), errors="coerce")
