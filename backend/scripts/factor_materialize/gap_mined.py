"""gap_mined 自有因子每日物化（L2 变换 / 行业 / 概念 / 两融 / 股东 / 财务）。

口径来源：2026-10 对 ``6_ml_datasets/gap_mined`` 存量 77 列逐列反解并在
金样日 20260911（L2/MG2/FIN/HD）与 20260828（IND/CONCEPT）上验证。

- **L2 变换**（50 列）：基列来自 ``l2_factors``，``traded`` = 当日 close 非空；
  ``_MA3`` = 63 个 traded 值均值（min_periods=1）；``_Z12`` = 252 个 traded 值
  的时序 z（ddof=1，``min_periods=126`` 半窗）；``_D3`` = 当日值 − 63 个 traded
  值前的值；``_RKCHG`` = 当日截面 pct 排名 − 该股 63 个 traded 值前那一天的
  截面 pct 排名。Z12 的均值/标准差是窗口内非空值上的统计量，与门限无关；门限
  取半窗是因为部分基列（如 ``micro_vpin_bayesian``/``micro_vpin_kyle``）在 L2
  历史分区前段整列为空，满窗门限会把整列结构性置空（金样 20260911 实测：
  满窗 0 个非空，半窗 5180，金样 5136；逐值仍 100% 一致）。
- **IND**：行业 = ``sector_members`` 的「行业板块(一级)」；指数 = 成员日收益
  （daily_backward close 的 pct_change）等权日再平衡复利；``IND_MOM`` = 指数
  21/63 日收益；``IND_REL_3M`` = 个股 63 日收益 − 行业指数 63 日收益；
  ``IND_RANK_w`` = 行业内个股 w 日收益的 pct 排名。
- **CONCEPT**：同法按「概念板块」成员造指数；``CONCEPT_MOM_3M_MEAN`` = 个股
  所属各概念 63 日收益的均值；``CONCEPT_REL_3M`` = 个股 63 日收益 − 该均值；
  ``CONCEPT_REL_1M`` 同理用 21 日。
- **MG2_BALCHG_3M**：两融 ``finance_balance`` 在两融日历上 ``shift(63)`` 后取比值 −1。
- **HD_***：股东户数 ``m_anntime <= 当月首个交易日`` 的最新报告；环比按报告序列
  shift(1)，同比按季度网格 shift(4)，``HD_NUM_Z`` 为季度网格最近 8 期 z（ddof=1）。
  HD 列在月内广播（与存量库一致：09 月各分区完全同值）。
- **FIN_FIXASSET_R**：``fix_assets / tot_assets``，同样按月首交易日快照广播。
- **NEWS_***：历史列由 Huntly 新闻库按发布时间生成，本仓库无法在本链路重建，
  新分区写 NaN（无任何现役模型引用这三列，见 ``REQUIRED_COLUMNS``）。

证伪纪律：金样日 L2/MG2 必须逐值一致；HD/IND/CONCEPT 的残余差异只允许来自
成分表刷新与财务数据商改版（本模块把差异计入报告，不静默）。

输出只写 staging 根（``--out``），官方库合并走 ``merge.py`` 的列级合并。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

try:
    from backend.scripts.factor_materialize import gap_mined_inputs as inputs
except ModuleNotFoundError:  # 直接执行本文件时补齐仓库根目录
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from backend.scripts.factor_materialize import gap_mined_inputs as inputs

LOG = logging.getLogger("factor_materialize.gap_mined")

GAP_DIR = "6_ml_datasets/gap_mined"
METADATA_COLUMNS = ("symbol", "date")
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
NEWS_COLUMNS = ("NEWS_N5", "NEWS_N20", "NEWS_PM")

#: 20 个 L2 基列 → 存量库保留的变换后缀（合计 50 列）。
L2_TRANSFORMS: dict[str, tuple[str, ...]] = {
    "flow_buy_amount": ("MA3", "RKCHG", "D3", "Z12"),
    "micro_jump_count_05pct": ("MA3", "Z12"),
    "micro_jump_recovery_time": ("MA3", "Z12", "RKCHG"),
    "micro_trade_interval_cv": ("RKCHG", "Z12"),
    "micro_vpin_amount_ratio": ("RKCHG", "Z12", "MA3"),
    "micro_vpin_bayesian": ("D3", "Z12", "MA3"),
    "micro_vpin_kyle": ("D3", "Z12"),
    "micro_vpin_ma_5": ("Z12", "MA3"),
    "micro_vpin_ma_20": ("Z12", "RKCHG"),
    "micro_vpin_vol_ratio": ("MA3", "Z12", "RKCHG", "D3"),
    "micro_vpin_zscore_20": ("Z12", "MA3", "D3"),
    "micro_zone_distribution": ("MA3",),
    "micro_zone_vpin_open": ("Z12", "D3", "MA3"),
    "vol_persistence": ("Z12", "RKCHG", "MA3"),
    "vol_realized_closing": ("MA3",),
    "vol_realized_jump": ("MA3",),
    "vol_realized_pm": ("MA3", "Z12", "RKCHG", "D3"),
    "vol_realized_rrv": ("MA3", "Z12", "RKCHG"),
    "vol_realized_rv": ("Z12", "RKCHG", "D3"),
    "vol_price_divergence": ("MA3",),
}

TRANSFORM_COLUMNS = tuple(
    f"{base}_{suffix}"
    for base, suffixes in L2_TRANSFORMS.items()
    for suffix in suffixes
)

NON_L2_COLUMNS = frozenset(
    {
        *NEWS_COLUMNS,
        "HD_NUMCHG_Q",
        "HD_NUMCHG_YOY",
        "HD_NUM_Z",
        "HD_PERNUM_LN",
        "HD_TOP1_CONC",
        "HD_ACTIVE_SHARE",
        "IND_MOM_1M",
        "IND_MOM_3M",
        "IND_REL_3M",
        "IND_RANK_1M",
        "IND_RANK_3M",
        "CONCEPT_MOM_3M_MEAN",
        "CONCEPT_REL_1M",
        "CONCEPT_REL_3M",
        "MG2_BALCHG_3M",
        "FIN_FIXASSET_R",
    }
)

#: 现役模型 ``factor_field_sources`` 实际引用的 43 列（覆盖率闸门用）。
REQUIRED_COLUMNS = (
    "CONCEPT_REL_1M",
    "CONCEPT_REL_3M",
    "HD_NUMCHG_YOY",
    "HD_NUM_Z",
    "IND_MOM_3M",
    "MG2_BALCHG_3M",
    "flow_buy_amount_D3",
    "flow_buy_amount_RKCHG",
    "micro_jump_count_05pct_MA3",
    "micro_jump_count_05pct_Z12",
    "micro_jump_recovery_time_MA3",
    "micro_jump_recovery_time_RKCHG",
    "micro_jump_recovery_time_Z12",
    "micro_trade_interval_cv_RKCHG",
    "micro_trade_interval_cv_Z12",
    "micro_vpin_amount_ratio_MA3",
    "micro_vpin_amount_ratio_RKCHG",
    "micro_vpin_amount_ratio_Z12",
    "micro_vpin_bayesian_D3",
    "micro_vpin_kyle_D3",
    "micro_vpin_ma_20_RKCHG",
    "micro_vpin_ma_20_Z12",
    "micro_vpin_vol_ratio_D3",
    "micro_vpin_vol_ratio_RKCHG",
    "micro_vpin_vol_ratio_Z12",
    "micro_vpin_zscore_20_D3",
    "micro_vpin_zscore_20_Z12",
    "micro_zone_distribution_MA3",
    "micro_zone_vpin_open_D3",
    "micro_zone_vpin_open_Z12",
    "vol_persistence_MA3",
    "vol_persistence_RKCHG",
    "vol_persistence_Z12",
    "vol_realized_closing_MA3",
    "vol_realized_jump_MA3",
    "vol_realized_pm_D3",
    "vol_realized_pm_RKCHG",
    "vol_realized_pm_Z12",
    "vol_realized_rrv_MA3",
    "vol_realized_rrv_RKCHG",
    "vol_realized_rrv_Z12",
    "vol_realized_rv_RKCHG",
    "vol_realized_rv_Z12",
)

LOOKBACK_L2_DAYS = 540
LOOKBACK_SECTOR_DAYS = 220
TRANSFORM_SHIFT = 63
ZSCORE_WINDOW = 252
# 半窗门限：Z12 的均值/标准差只取窗口内非空值，门限仅决定是否出值；
# 满窗会在基列前段整列为空的标的上结构性置空（金样 20260911 已证伪）。
ZSCORE_MIN_PERIODS = ZSCORE_WINDOW // 2
MA3_WINDOW = 63
MG2_LAG = 63
IND_MOM_1M = 21
IND_MOM_3M = 63

# 上游两融数据缺分区（当日未同步）时允许 MG2 当日写 NaN；次日补数后由
# 编排器按 required_empty 回补。其余必填列全空一律视为物化缺陷并中止。
EMPTY_TOLERANT_COLUMNS = frozenset({"MG2_BALCHG_3M"})


# --------------------------------------------------------------------- 日历


def partition_dates(root: Path) -> list[str]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        p.name[3:] for p in root.iterdir() if p.is_dir() and p.name.startswith("dt=")
    )


def _shift_date(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _month_first_trading_day(calendar: list[str], month: str) -> str:
    for date in calendar:
        if date[:6] == month:
            return date
    raise ValueError(f"交易日历中没有 {month} 的交易日")


def template_columns(quantdb_root: Path) -> list[str]:
    """以官方库最新分区为 schema 模板，并校验列集合完全可解释。"""
    root = Path(quantdb_root) / GAP_DIR
    dates = partition_dates(root)
    if not dates:
        raise FileNotFoundError(f"官方 gap_mined 库为空：{root}")
    path = root / f"dt={dates[-1]}" / "data.parquet"
    columns = list(pq.ParquetFile(path).schema_arrow.names)
    known = (
        set(METADATA_COLUMNS)
        | set(OHLCV_COLUMNS)
        | set(TRANSFORM_COLUMNS)
        | NON_L2_COLUMNS
    )
    unknown = sorted(set(columns) - known)
    missing = sorted(known - set(columns))
    if unknown or missing:
        raise ValueError(
            f"gap_mined schema 与物化口径不一致：unknown={unknown} missing={missing}"
        )
    return columns


def target_dates(quantdb_root: Path, start: str, end: str) -> list[str]:
    root = Path(quantdb_root)
    l2_dates = set(partition_dates(root / inputs.L2_DIR))
    backward_dates = set(partition_dates(root / inputs.BACKWARD_DIR))
    dates = sorted(d for d in l2_dates if start <= d <= end)
    if not dates:
        raise ValueError(f"{start}..{end} 没有 l2_factors 分区")
    missing = [d for d in dates if d not in backward_dates]
    if missing:
        raise ValueError(f"l2_factors 有分区但 daily_backward 缺日：{missing[:5]}")
    return dates


# ----------------------------------------------------------------- 变换核心


def traded_shift(
    wide: pd.DataFrame, traded: pd.DataFrame, periods: int
) -> pd.DataFrame:
    """按每个标的自己的 traded 行序列做 shift（停牌日不占位）。"""
    values = wide.to_numpy(dtype="float64", copy=False)
    mask = traded.to_numpy(dtype=bool, copy=False)
    out = np.full_like(values, np.nan)
    for column in range(values.shape[1]):
        rows = mask[:, column]
        count = int(rows.sum())
        if count == 0:
            continue
        series = values[rows, column]
        shifted = np.full(count, np.nan)
        if count > periods:
            shifted[periods:] = series[:-periods]
        column_values = out[:, column]
        column_values[rows] = shifted
        out[:, column] = column_values
    return pd.DataFrame(out, index=wide.index, columns=wide.columns)


def traded_rolling(
    wide: pd.DataFrame,
    traded: pd.DataFrame,
    window: int,
    *,
    min_periods: int,
    zscore: bool = False,
) -> pd.DataFrame:
    """按 traded 行序列做滚动均值；``zscore`` 时返回 (x−mean)/std(ddof=1)。"""
    values = wide.to_numpy(dtype="float64", copy=False)
    mask = traded.to_numpy(dtype=bool, copy=False)
    out = np.full_like(values, np.nan)
    for column in range(values.shape[1]):
        rows = mask[:, column]
        if not rows.any():
            continue
        series = pd.Series(values[rows, column])
        rolling = series.rolling(window, min_periods=min_periods)
        if zscore:
            result = (series - rolling.mean()) / rolling.std(ddof=1)
        else:
            result = rolling.mean()
        column_values = out[:, column]
        column_values[rows] = result.to_numpy()
        out[:, column] = column_values
    return pd.DataFrame(out, index=wide.index, columns=wide.columns)


def compute_l2_transforms(
    frames: dict[str, pd.DataFrame],
    close: pd.DataFrame,
    dates: list[str],
) -> dict[str, pd.DataFrame]:
    traded = close.notna()
    result: dict[str, pd.DataFrame] = {}
    for base, suffixes in L2_TRANSFORMS.items():
        wide = frames[base]
        for suffix in suffixes:
            if suffix == "MA3":
                full = traded_rolling(wide, traded, MA3_WINDOW, min_periods=1)
            elif suffix == "Z12":
                full = traded_rolling(
                    wide,
                    traded,
                    ZSCORE_WINDOW,
                    min_periods=ZSCORE_MIN_PERIODS,
                    zscore=True,
                )
            elif suffix == "D3":
                full = wide - traded_shift(wide, traded, TRANSFORM_SHIFT)
            elif suffix == "RKCHG":
                ranks = wide.rank(axis=1, pct=True)
                full = ranks - traded_shift(ranks, traded, TRANSFORM_SHIFT)
            else:  # pragma: no cover - 由 L2_TRANSFORMS 常量保证
                raise ValueError(f"未知变换后缀：{suffix}")
            result[f"{base}_{suffix}"] = full.reindex(index=dates)
    return result


def _equal_weight_index(close: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """成员日收益等权（跳过无效成员）日再平衡复利指数。"""
    columns = members.columns.intersection(close.columns)
    matrix = members[columns].to_numpy(dtype=bool)
    returns = close[columns].pct_change(fill_method=None).to_numpy(dtype="float64")
    valid = np.isfinite(returns)
    member_float = matrix.astype("float64").T
    sums = np.nan_to_num(returns, nan=0.0, posinf=0.0, neginf=0.0) @ member_float
    counts = valid.astype("float64") @ member_float
    mean = np.divide(sums, counts, out=np.full(sums.shape, np.nan), where=counts > 0)
    index = np.cumprod(1.0 + np.nan_to_num(mean, nan=0.0), axis=0)
    return pd.DataFrame(index, index=close.index, columns=members.index)


def _mean_over_members(
    values: np.ndarray, members: np.ndarray, index: pd.Index, columns: pd.Index
) -> pd.DataFrame:
    """把「成员组 × 日期」的值按股票的成员组集合取均值。"""
    valid = np.isfinite(values)
    member_float = members.astype("float64")
    sums = np.nan_to_num(values, nan=0.0) @ member_float
    counts = valid.astype("float64") @ member_float
    mean = np.divide(sums, counts, out=np.full(sums.shape, np.nan), where=counts > 0)
    return pd.DataFrame(mean, index=index, columns=columns)


def compute_sector_factors(
    close: pd.DataFrame,
    industry: pd.DataFrame,
    concept: pd.DataFrame,
    dates: list[str],
) -> dict[str, pd.DataFrame]:
    stock_r1 = close / close.shift(IND_MOM_1M) - 1.0
    stock_r3 = close / close.shift(IND_MOM_3M) - 1.0

    industry = industry.reindex(columns=close.columns, fill_value=False)
    ind_index = _equal_weight_index(close, industry)
    ind_mom1 = (ind_index / ind_index.shift(IND_MOM_1M) - 1.0).to_numpy()
    ind_mom3 = (ind_index / ind_index.shift(IND_MOM_3M) - 1.0).to_numpy()
    ind_members = industry.to_numpy(dtype=bool)
    has_industry = ind_members.any(axis=0)

    def _to_stock(values: np.ndarray) -> pd.DataFrame:
        mapped = values @ ind_members.astype("float64")
        mapped[:, ~has_industry] = np.nan
        return pd.DataFrame(mapped, index=close.index, columns=close.columns)

    ind_mom1_stock = _to_stock(ind_mom1)
    ind_mom3_stock = _to_stock(ind_mom3)
    ind_rank1 = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    ind_rank3 = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for code in industry.index:
        symbols = industry.columns[industry.loc[code].to_numpy(dtype=bool)]
        symbols = symbols.intersection(close.columns)
        if symbols.empty:
            continue
        ind_rank1[symbols] = stock_r1[symbols].rank(axis=1, pct=True)
        ind_rank3[symbols] = stock_r3[symbols].rank(axis=1, pct=True)

    concept = concept.reindex(columns=close.columns, fill_value=False)
    concept_index = _equal_weight_index(close, concept)
    concept_mom1 = (concept_index / concept_index.shift(IND_MOM_1M) - 1.0).to_numpy()
    concept_mom3 = (concept_index / concept_index.shift(IND_MOM_3M) - 1.0).to_numpy()
    concept_members = concept.to_numpy(dtype=bool)
    concept_mean1 = _mean_over_members(
        concept_mom1, concept_members, close.index, close.columns
    )
    concept_mean3 = _mean_over_members(
        concept_mom3, concept_members, close.index, close.columns
    )

    result = {
        "IND_MOM_1M": ind_mom1_stock,
        "IND_MOM_3M": ind_mom3_stock,
        "IND_REL_3M": stock_r3 - ind_mom3_stock,
        "IND_RANK_1M": ind_rank1,
        "IND_RANK_3M": ind_rank3,
        "CONCEPT_MOM_3M_MEAN": concept_mean3,
        "CONCEPT_REL_1M": stock_r1 - concept_mean1,
        "CONCEPT_REL_3M": stock_r3 - concept_mean3,
    }
    return {name: frame.reindex(index=dates) for name, frame in result.items()}


def compute_mg2(
    quantdb_root: Path, start: str, end: str, dates: list[str]
) -> pd.DataFrame:
    balance = inputs.load_margin_balance(quantdb_root, start, end)
    return (balance / balance.shift(MG2_LAG) - 1.0).reindex(index=dates)


# --------------------------------------------------------------------- 装配


def _ohlcv_by_date(
    l2_long: pd.DataFrame, backward_long: pd.DataFrame, date: str
) -> tuple[pd.Index, pd.DataFrame]:
    """gap 股票池 = l2 ∪ daily_backward；OHLCV 优先 l2，缺失标的回退 backward。"""
    columns = list(OHLCV_COLUMNS)
    l2_day = l2_long[l2_long["dt"] == date].set_index("symbol")[columns]
    backward_day = backward_long[backward_long["dt"] == date].set_index("symbol")[
        columns
    ]
    universe = l2_day.index.union(backward_day.index).sort_values()
    ohlcv = l2_day.reindex(universe).combine_first(backward_day.reindex(universe))
    return universe, ohlcv


def tolerated_empty(empty: list[str], *, margin_available: bool) -> list[str]:
    """返回可容忍的空列：仅当上游两融分区缺失时放行 MG2。"""
    if margin_available:
        return []
    return [column for column in empty if column in EMPTY_TOLERANT_COLUMNS]


def _factor_row(
    date: str,
    universe: pd.Index,
    matrices: dict[str, pd.DataFrame],
    financial: pd.DataFrame,
) -> pd.DataFrame:
    row = pd.DataFrame(index=universe)
    for column, matrix in matrices.items():
        if date in matrix.index:
            values = matrix.loc[date].reindex(universe)
        else:
            values = pd.Series(np.nan, index=universe)
        row[column] = values.to_numpy()
    for column in financial.columns:
        row[column] = financial[column].reindex(universe).to_numpy()
    return row


def assemble_partition(
    date: str,
    universe: pd.Index,
    ohlcv: pd.DataFrame,
    factors: pd.DataFrame,
    template: list[str],
) -> pd.DataFrame:
    frame = pd.DataFrame({"symbol": pd.array(universe, dtype="str")})
    frame["date"] = pd.to_datetime(date, format="%Y%m%d").as_unit("ns")
    for column in template:
        if column in METADATA_COLUMNS:
            continue
        if column in OHLCV_COLUMNS:
            frame[column] = ohlcv[column].reindex(universe).to_numpy()
        elif column in NEWS_COLUMNS:
            frame[column] = np.nan
        else:
            frame[column] = factors[column].reindex(universe).to_numpy()
    return frame[template]


def build(
    quantdb_root: Path,
    out_root: Path,
    start: str,
    end: str,
    *,
    dates: list[str] | None = None,
    write: bool = True,
) -> list[dict]:
    started = time.time()
    quantdb_root = Path(quantdb_root)
    out_root = Path(out_root)
    template = template_columns(quantdb_root)
    dates = dates or target_dates(quantdb_root, start, end)
    calendar = partition_dates(quantdb_root / inputs.L2_DIR)
    month_cutoffs = {
        month: _month_first_trading_day(calendar, month)
        for month in sorted({date[:6] for date in dates})
    }
    LOG.info(
        "目标 %d 日（%s..%s），月快照：%s",
        len(dates),
        dates[0],
        dates[-1],
        month_cutoffs,
    )

    l2_start = _shift_date(dates[0], LOOKBACK_L2_DAYS)
    sector_start = _shift_date(dates[0], LOOKBACK_SECTOR_DAYS)
    frames, l2_close = inputs.load_l2_window(
        quantdb_root, l2_start, dates[-1], list(L2_TRANSFORMS)
    )
    matrices = compute_l2_transforms(frames, l2_close, dates)
    del frames, l2_close
    LOG.info("L2 变换完成：%d 列（%.1fs）", len(matrices), time.time() - started)

    backward_close = inputs.load_backward_close(quantdb_root, sector_start, dates[-1])
    members = inputs.load_sector_members(quantdb_root)
    matrices.update(
        compute_sector_factors(backward_close, members.industry, members.concept, dates)
    )
    del backward_close
    LOG.info("IND/CONCEPT 完成（%.1fs）", time.time() - started)

    matrices["MG2_BALCHG_3M"] = compute_mg2(quantdb_root, l2_start, dates[-1], dates)
    LOG.info("MG2 完成（%.1fs）", time.time() - started)

    financial_by_month = {
        month: inputs.holder_snapshot(quantdb_root, cutoff).join(
            inputs.balance_snapshot(quantdb_root, cutoff), how="outer"
        )
        for month, cutoff in month_cutoffs.items()
    }
    LOG.info("HD/FIN 月快照完成（%.1fs）", time.time() - started)

    l2_long = inputs.load_l2_ohlcv(quantdb_root, dates)
    backward_long = inputs.load_backward_ohlcv(quantdb_root, dates)

    summaries = []
    for date in dates:
        universe, ohlcv = _ohlcv_by_date(l2_long, backward_long, date)
        factors = _factor_row(date, universe, matrices, financial_by_month[date[:6]])
        partition = assemble_partition(date, universe, ohlcv, factors, template)
        required_coverage = {
            column: int(partition[column].notna().sum()) for column in REQUIRED_COLUMNS
        }
        empty = [column for column, count in required_coverage.items() if count == 0]
        tolerated = tolerated_empty(
            empty, margin_available=inputs.has_margin_partition(quantdb_root, date)
        )
        fatal = [column for column in empty if column not in tolerated]
        if fatal:
            # 先判后写：必填列全空的分区绝不落 staging（合并不区分致命与否）
            raise RuntimeError(f"{date} 必填因子列全空：{fatal}")
        path = out_root / f"dt={date}" / "data.parquet"
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            partition.to_parquet(path, index=False)
        summary = {
            "date": date,
            "rows": len(partition),
            "l2_symbols": int((l2_long["dt"] == date).sum()),
            "backward_symbols": int((backward_long["dt"] == date).sum()),
            "required_min_coverage": min(required_coverage.values(), default=0),
            "required_empty": empty,
            "tolerated_empty": tolerated,
            "written": str(path) if write else None,
        }
        summaries.append(summary)
        LOG.info(
            "%s rows=%d required_min=%d empty=%s",
            date,
            summary["rows"],
            summary["required_min_coverage"],
            empty,
        )
        if tolerated:
            LOG.warning(
                "%s 上游两融分区缺失，暂写 NaN（次日补数后回补）：%s", date, tolerated
            )
        if fatal:
            raise RuntimeError(f"{date} 必填因子列全空：{fatal}")

    LOG.info("完成 %d 个分区（%.1fs）", len(summaries), time.time() - started)
    return summaries


# --------------------------------------------------------------------- 校验


def _column_group(column: str) -> str:
    if column in OHLCV_COLUMNS:
        return "OHLCV"
    if column in NEWS_COLUMNS:
        return "NEWS"
    if column in TRANSFORM_COLUMNS:
        return "L2"
    if column == "MG2_BALCHG_3M":
        return "MG2"
    if column.startswith("HD_"):
        return "HD"
    if column.startswith("IND_"):
        return "IND"
    if column.startswith("CONCEPT_"):
        return "CONCEPT"
    if column == "FIN_FIXASSET_R":
        return "FIN"
    return "OTHER"


def compare_partition(official_path: Path, candidate_path: Path) -> dict:
    gold = pd.read_parquet(official_path).set_index("symbol")
    cand = pd.read_parquet(candidate_path).set_index("symbol")
    if list(gold.columns) != list(cand.columns):
        raise ValueError("列集合/顺序不一致，按结构性失败处理")
    common = gold.index.intersection(cand.index)
    missing_symbols = gold.index.difference(cand.index)
    extra_symbols = cand.index.difference(gold.index)
    columns: dict[str, dict] = {}
    for column in gold.columns:
        both = pd.concat(
            [
                cand.loc[common, column].rename("c"),
                gold.loc[common, column].rename("g"),
            ],
            axis=1,
        ).dropna()
        if both.empty:
            columns[column] = {
                "group": _column_group(column),
                "n": 0,
                "exact": None,
                "maxabs": None,
                "cand_nonnull": int(cand[column].notna().sum()),
                "gold_nonnull": int(gold[column].notna().sum()),
            }
            continue
        c = both["c"].to_numpy(dtype="float64")
        g = both["g"].to_numpy(dtype="float64")
        exact = float(np.isclose(c, g, rtol=1e-5, atol=1e-7).mean())
        finite = np.isfinite(c) & np.isfinite(g)
        maxabs = float(np.abs(c[finite] - g[finite]).max()) if finite.any() else 0.0
        columns[column] = {
            "group": _column_group(column),
            "n": len(both),
            "exact": exact,
            "maxabs": maxabs,
            "cand_nonnull": int(cand[column].notna().sum()),
            "gold_nonnull": int(gold[column].notna().sum()),
        }
    groups: dict[str, dict] = {}
    for stats in columns.values():
        bucket = groups.setdefault(stats["group"], {"columns": 0, "exact": []})
        bucket["columns"] += 1
        if stats["exact"] is not None:
            bucket["exact"].append(stats["exact"])
    for bucket in groups.values():
        values = bucket.pop("exact")
        bucket["min_exact"] = min(values) if values else None
        bucket["mean_exact"] = float(np.mean(values)) if values else None
    return {
        "official_rows": len(gold),
        "candidate_rows": len(cand),
        "missing_symbols": len(missing_symbols),
        "extra_symbols": len(extra_symbols),
        "groups": groups,
        "columns": columns,
    }


def _print_report(report: dict) -> None:
    print(
        f"official_rows={report['official_rows']} candidate_rows={report['candidate_rows']} "
        f"missing_symbols={report['missing_symbols']} extra_symbols={report['extra_symbols']}"
    )
    print(f"{'group':10s} {'cols':>4s} {'min_exact':>10s} {'mean_exact':>11s}")
    for group in sorted(report["groups"]):
        stats = report["groups"][group]

        def _fmt(value):
            return "n/a" if value is None else f"{value:.6f}"

        print(
            f"{group:10s} {stats['columns']:4d} {_fmt(stats['min_exact']):>10s} "
            f"{_fmt(stats['mean_exact']):>11s}"
        )
    weak = [
        (column, stats)
        for column, stats in report["columns"].items()
        if stats["exact"] is not None and stats["exact"] < 0.99
    ]
    weak.sort(key=lambda item: item[1]["exact"])
    if weak:
        print("\n逐列 exact < 0.99：")
        for column, stats in weak[:30]:
            print(
                f"  {column:32s} {stats['group']:8s} exact={stats['exact']:.6f} "
                f"n={stats['n']} maxabs={stats['maxabs']:.3e} "
                f"cand_nn={stats['cand_nonnull']} gold_nn={stats['gold_nonnull']}"
            )


def verify(
    quantdb_root: Path,
    out_root: Path,
    date: str,
    *,
    rebuild: bool = True,
) -> dict:
    official = Path(quantdb_root) / GAP_DIR / f"dt={date}" / "data.parquet"
    if not official.is_file():
        raise FileNotFoundError(f"官方分区不存在：{official}")
    candidate = Path(out_root) / f"dt={date}" / "data.parquet"
    if rebuild or not candidate.is_file():
        build(
            quantdb_root,
            out_root,
            date,
            date,
            dates=[date],
            write=True,
        )
    report = compare_partition(official, candidate)
    report["date"] = date
    return report


# ------------------------------------------------------------------------ CLI


def _default_staging(quantdb_root: Path) -> Path:
    return Path(quantdb_root) / "6_ml_datasets" / "_staging_gap_mined"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gap_mined 自有因子每日物化")
    parser.add_argument(
        "--quantdb-root",
        type=Path,
        default=None,
        help="QuantDB 根（默认 QM_QUANTDB_DATA_DIR 或 data/quantdb）",
    )
    parser.add_argument("--out", type=Path, default=None, help="staging 输出根")
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser("build", help="物化分区到 staging")
    build_parser.add_argument("--start", required=True)
    build_parser.add_argument("--end", required=True)
    build_parser.add_argument("--dry-run", action="store_true")

    verify_parser = sub.add_parser("verify", help="构建并与官方分区逐列比对")
    verify_parser.add_argument("--date", required=True)
    verify_parser.add_argument("--json", type=Path, default=None)
    verify_parser.add_argument("--strict", action="store_true")
    verify_parser.add_argument("--no-build", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    import os

    if args.quantdb_root is None:
        env_root = os.environ.get("QM_QUANTDB_DATA_DIR")
        quantdb_root = Path(env_root) if env_root else Path("data/quantdb")
    else:
        quantdb_root = args.quantdb_root
    out_root = args.out or _default_staging(quantdb_root)

    if args.command == "build":
        summaries = build(
            quantdb_root,
            out_root,
            args.start,
            args.end,
            write=not args.dry_run,
        )
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0

    report = verify(quantdb_root, out_root, args.date, rebuild=not args.no_build)
    _print_report(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.strict:
        failures = []
        structural = (
            report["missing_symbols"]
            or report["extra_symbols"]
            or report["official_rows"] != report["candidate_rows"]
        )
        if structural:
            failures.append("行集合不一致")
        for group in ("L2", "MG2"):
            exact = report["groups"].get(group, {}).get("min_exact")
            if exact is None or exact < 0.999:
                failures.append(f"{group} min_exact={exact}")
        if failures:
            print("STRICT FAILED: " + "；".join(failures))
            return 1
        print("STRICT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
