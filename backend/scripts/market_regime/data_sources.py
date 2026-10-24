"""大盘/行业预测模型的增量数据源（2026-10-24 v2）。

新增数据（全部落 parquet，带 provenance）：
1. 申万一级行业指数日线（官方指数，PIT 由申万维护，天然无幸存者偏差）；
2. 申万个股行业分类变迁史（PIT 成分审计表）；
3. 宏观日频：SHIBOR、国债收益率、USD/CNY；
4. 沪市 ETF 份额月度快照（份额变化 → 资金流代理）；
5. 股指期货基差：由本地 `futures_daily.parquet` 主力连续 + 指数现货合成。

数据边界：
- `index_hist_sw` 覆盖 1999-12 ~ 至今；本模块默认只保留 `--start` 之后。
- ETF 份额只有沪市可查历史（`fund_etf_scale_sse(date=...)`）；深交所接口只返回
  当前快照，故份额序列明确标注为「沪市 ETF」。
- 申万分类文件下载源 `swsresearch.com` 证书链不完整，`requests` 默认校验会失败；
  该文件仅作为数据解析（不执行），下载时关闭校验并在报告标注。

用法：
  python backend/scripts/market_regime/data_sources.py --all
  python backend/scripts/market_regime/data_sources.py --sw-indices --macro --etf --membership
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
import warnings
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("/data/market_regime")
EXTERNAL_SUBDIR = "external"

# 沪市境内股票 ETF 代码前缀（排除 511 货币/债券、513 跨境、518 黄金）
EQUITY_ETF_PREFIXES = (
    "510",
    "512",
    "515",
    "516",
    "517",
    "560",
    "561",
    "562",
    "563",
    "588",
    "589",
)

BENCHMARK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "csi300": ("沪深300",),
    "csi500": ("中证500",),
    "csi1000": ("中证1000",),
    "csi50": ("上证50",),
    "star50": ("科创50", "科创板50"),
}

INDEX_FUTURES_SPOT: dict[str, str] = {
    "IF": "000300.SH",
    "IH": "000016.SH",
    "IC": "000905.SH",
    "IM": "000852.SH",
}

SW_CLASSIFY_URL = (
    "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
)


def _retry(fn: Callable[[], Any], *, tries: int = 3, delay: float = 1.0) -> Any:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络源不稳定，统一重试
            last = exc
            if attempt < tries - 1:
                time.sleep(delay * (attempt + 1))
    assert last is not None
    raise last


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _external_dir(out_dir: Path) -> Path:
    path = out_dir / EXTERNAL_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_sw_l1_indices(
    out_dir: Path = DEFAULT_DIR, *, start: str = "2015-01-01", force: bool = False
) -> Path:
    """申万一级行业官方指数日线（31 条）。

    用官方指数而不是「当前成分合成」：申万指数成分由其维护，历史序列本身
    就是 PIT 口径，从根本上消除幸存者偏差。
    """
    import akshare as ak

    ext = _external_dir(out_dir)
    out = ext / "sw_l1_index_daily.parquet"
    if out.exists() and not force:
        logger.info("复用 %s", out)
        return out

    info = _retry(ak.sw_index_first_info)
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    for _, row in info.iterrows():
        full_code = str(row["行业代码"])
        code = full_code.split(".")[0]
        name = str(row["行业名称"])
        try:
            df = _retry(
                lambda code=code: ak.index_hist_sw(symbol=code, period="day")
            )
        except Exception as exc:  # noqa: BLE001 - 单行业失败不阻断
            logger.warning("申万指数 %s(%s) 拉取失败：%s", name, code, exc)
            failed.append(full_code)
            continue
        if df.empty:
            failed.append(full_code)
            continue
        df = df.rename(
            columns={
                "日期": "trade_date",
                "收盘": "close",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
        df["sector_code"] = full_code
        df["sector_name"] = name
        df = df[df["trade_date"] >= pd.Timestamp(start)]
        frames.append(
            df[
                [
                    "trade_date",
                    "sector_code",
                    "sector_name",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                ]
            ]
        )
        time.sleep(0.05)

    if not frames:
        raise RuntimeError("申万一级行业指数全部拉取失败")
    full = pd.concat(frames, ignore_index=True).sort_values(
        ["sector_code", "trade_date"]
    )
    full.to_parquet(out, index=False)
    _write_meta(
        ext / "sw_l1_index_daily_meta.json",
        {
            "source": "akshare.index_hist_sw / swsresearch",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(full)),
            "sectors": int(full["sector_code"].nunique()),
            "start": str(full["trade_date"].min().date()),
            "end": str(full["trade_date"].max().date()),
            "failed": failed,
            "notes": [
                "官方申万一级行业指数日线，历史成分由申万维护（PIT）",
                "不是用当前成分合成，因此不存在幸存者偏差",
            ],
        },
    )
    logger.info("申万一级指数落盘：%s（%d 行）", out, len(full))
    return out


def fetch_sw_membership_pit(
    out_dir: Path = DEFAULT_DIR, *, force: bool = False
) -> Path:
    """申万个股行业分类变迁史（PIT 成分审计表）。

    文件列：股票代码 / 计入日期 / 行业代码 / 更新日期；同一股票多行 = 行业变更。
    `计入日期` 是生效日，可按 as-of 还原任意历史日的行业归属。
    """
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning

    ext = _external_dir(out_dir)
    out = ext / "sw_l1_membership_pit.parquet"
    if out.exists() and not force:
        logger.info("复用 %s", out)
        return out

    def _download() -> bytes:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            resp = requests.get(
                SW_CLASSIFY_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=90,
                verify=False,
            )
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError("申万分类文件为空")
        return resp.content

    content = _retry(_download)
    raw = pd.read_excel(
        io.BytesIO(content), dtype={"股票代码": str, "行业代码": str}
    )
    raw = raw.rename(
        columns={
            "股票代码": "symbol",
            "计入日期": "start_date",
            "行业代码": "industry_code",
            "更新日期": "update_time",
        }
    )
    raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)
    raw["industry_code"] = raw["industry_code"].astype(str).str.zfill(6)
    raw["start_date"] = pd.to_datetime(raw["start_date"], errors="coerce")
    raw["update_time"] = pd.to_datetime(raw["update_time"], errors="coerce")
    raw = raw.dropna(subset=["symbol", "start_date", "industry_code"]).copy()
    raw["industry_l1"] = raw["industry_code"].str[:2]

    from backend.shared.stock_utils import StockCodeUtil

    raw["symbol_prefix"] = raw["symbol"].map(
        lambda s: StockCodeUtil.to_prefix(str(s))
    )
    raw = raw.sort_values(["symbol_prefix", "start_date", "industry_code"]).reset_index(
        drop=True
    )
    raw[
        [
            "symbol",
            "symbol_prefix",
            "start_date",
            "industry_code",
            "industry_l1",
            "update_time",
        ]
    ].to_parquet(out, index=False)
    _write_meta(
        ext / "sw_l1_membership_pit_meta.json",
        {
            "source": SW_CLASSIFY_URL,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(raw)),
            "stocks": int(raw["symbol_prefix"].nunique()),
            "start": str(raw["start_date"].min().date()),
            "end": str(raw["start_date"].max().date()),
            "notes": [
                "PIT 行业归属：任一日期取 start_date<=该日的最后一条",
                "行业代码前 2 位为申万一级；分类版本（2014/2021）切换时前缀含义可能变化",
                "该文件下载源证书链不完整，抓取时关闭 TLS 校验；仅作为数据解析，不执行",
            ],
        },
    )
    logger.info("申万行业变迁史落盘：%s（%d 行）", out, len(raw))
    return out


def fetch_macro_daily(
    out_dir: Path = DEFAULT_DIR, *, start: str = "2015-01-01", force: bool = False
) -> Path:
    """宏观日频：SHIBOR、国债收益率（中/美）、USD/CNY。"""
    import akshare as ak

    ext = _external_dir(out_dir)
    out = ext / "macro_daily.parquet"
    if out.exists() and not force:
        logger.info("复用 %s", out)
        return out

    frames: list[pd.DataFrame] = []

    shibor_frames: list[pd.DataFrame] = []
    for indicator, col in (
        ("隔夜", "shibor_on"),
        ("1周", "shibor_1w"),
        ("1月", "shibor_1m"),
        ("3月", "shibor_3m"),
        ("1年", "shibor_1y"),
    ):
        part = _retry(
            lambda ind=indicator: ak.rate_interbank(
                market="上海银行同业拆借市场",
                symbol="Shibor人民币",
                indicator=ind,
            )
        )
        part = part.rename(columns={"报告日": "trade_date", "利率": col})
        part["trade_date"] = pd.to_datetime(part["trade_date"], errors="coerce")
        shibor_frames.append(
            part[["trade_date", col]]
            .dropna(subset=["trade_date"])
            .sort_values("trade_date")
        )
    shibor = shibor_frames[0]
    for frame in shibor_frames[1:]:
        shibor = shibor.merge(frame, on="trade_date", how="outer")
    frames.append(shibor.sort_values("trade_date"))

    bond = _retry(lambda: ak.bond_zh_us_rate(start_date="20150101"))
    bond = bond.rename(columns={"日期": "trade_date"})
    bond["trade_date"] = pd.to_datetime(bond["trade_date"], errors="coerce")
    bond = bond.rename(
        columns={
            "中国国债收益率2年": "cn_2y",
            "中国国债收益率10年": "cn_10y",
            "中国国债收益率30年": "cn_30y",
            "美国国债收益率2年": "us_2y",
            "美国国债收益率10年": "us_10y",
        }
    )
    frames.append(
        bond[["trade_date", "cn_2y", "cn_10y", "cn_30y", "us_2y", "us_10y"]]
        .dropna(subset=["trade_date"])
        .sort_values("trade_date")
    )

    fx = _retry(
        lambda: ak.currency_boc_sina(
            symbol="美元",
            start_date=start.replace("-", ""),
            end_date=date.today().strftime("%Y%m%d"),
        )
    )
    fx = fx.rename(columns={"日期": "trade_date", "中行折算价": "usdcny"})
    fx["trade_date"] = pd.to_datetime(fx["trade_date"], errors="coerce")
    frames.append(
        fx[["trade_date", "usdcny"]].dropna(subset=["trade_date"]).sort_values("trade_date")
    )

    macro = frames[0]
    for frame in frames[1:]:
        macro = macro.merge(frame, on="trade_date", how="outer")
    macro = macro[macro["trade_date"] >= pd.Timestamp(start)]
    macro = macro.drop_duplicates(subset=["trade_date"]).sort_values("trade_date")
    macro = macro.reset_index(drop=True)

    # 仅对周末/短假期做限幅前填（limit=5），长缺口保持 NaN，避免用陈旧值
    for col in (
        "shibor_on",
        "shibor_1w",
        "shibor_1m",
        "shibor_3m",
        "shibor_1y",
        "cn_2y",
        "cn_10y",
        "cn_30y",
        "us_2y",
        "us_10y",
        "usdcny",
    ):
        if col in macro.columns:
            macro[col] = macro[col].ffill(limit=5)

    # 派生：变化/利差/分位（只用当日及以前）
    macro["cn_term_spread"] = macro["cn_10y"] - macro["cn_2y"]
    macro["cn_us_10y_spread"] = macro["cn_10y"] - macro["us_10y"]
    for col in ("shibor_on", "shibor_3m", "cn_10y", "us_10y", "usdcny"):
        macro[f"{col}_chg_5"] = macro[col].diff(5)
        macro[f"{col}_z60"] = (
            macro[col] - macro[col].rolling(60, min_periods=30).mean()
        ) / macro[col].rolling(60, min_periods=30).std().replace(0, np.nan)

    macro.to_parquet(out, index=False)
    _write_meta(
        ext / "macro_daily_meta.json",
        {
            "source": "akshare: rate_interbank (SHIBOR) / bond_zh_us_rate / currency_boc_sina",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(macro)),
            "start": str(macro["trade_date"].min().date()),
            "end": str(macro["trade_date"].max().date()),
            "columns": macro.columns.tolist(),
            "notes": [
                "SHIBOR 与国债收益率为当日收盘后可观测值，用于 T 日收盘信号无前视",
                "USD/CNY 取中行折算价；周末/节假日为 NaN，训练时按缺失处理",
            ],
        },
    )
    logger.info("宏观日频落盘：%s（%d 行）", out, len(macro))
    return out


def _trading_days(start: str, end: str) -> pd.DatetimeIndex:
    from backend.shared.benchmark import load_index_frame

    df = load_index_frame(
        columns=("close",), symbols=["000300.SH"], start=start, end=end, market="CN"
    )
    return pd.DatetimeIndex(pd.to_datetime(df["time"]).dt.normalize().unique()).sort_values()


def fetch_etf_shares_monthly(
    out_dir: Path = DEFAULT_DIR, *, start: str = "2016-01-01", force: bool = False
) -> Path:
    """沪市 ETF 份额月度快照 + 聚合特征。

    交易所接口只接受「某日快照」，故按月取最后一个交易日；快照在当日收盘后
    才可得，合并到日频时统一用 `available_date=下一交易日`，避免前视。
    """
    import akshare as ak

    ext = _external_dir(out_dir)
    out = ext / "etf_shares_monthly.parquet"
    raw_out = ext / "etf_shares_snapshots.parquet"
    if out.exists() and not force:
        logger.info("复用 %s", out)
        return out

    days = _trading_days(start, date.today().isoformat())
    month_ends = pd.Series(days, index=days).groupby(days.to_period("M")).max()
    snapshots: list[pd.DataFrame] = []
    failed: list[str] = []
    for period, day in month_ends.items():
        fetched = None
        for back in range(8):
            probe = (day - pd.Timedelta(days=back)).normalize()
            try:
                df = ak.fund_etf_scale_sse(date=probe.strftime("%Y%m%d"))
                if not df.empty:
                    fetched = (probe, df)
                    break
            except Exception:  # noqa: BLE001 - 非交易日/接口抖动，回退一天
                continue
        if fetched is None:
            failed.append(str(period))
            continue
        probe, df = fetched
        df = df.copy()
        df["snapshot_date"] = pd.Timestamp(df["统计日期"].iloc[0])
        df["fund_code"] = df["基金代码"].astype(str).str.zfill(6)
        df = df.rename(
            columns={"基金简称": "fund_name", "ETF类型": "etf_type", "基金份额": "shares"}
        )
        snapshots.append(
            df[["snapshot_date", "fund_code", "fund_name", "etf_type", "shares"]]
        )
        time.sleep(0.1)

    if not snapshots:
        raise RuntimeError("ETF 份额月度快照全部失败")
    raw = pd.concat(snapshots, ignore_index=True)
    raw["is_equity"] = raw["fund_code"].str.startswith(EQUITY_ETF_PREFIXES)
    raw.to_parquet(raw_out, index=False)

    eq = raw[raw["is_equity"]].copy()
    agg = (
        eq.groupby("snapshot_date", as_index=False)
        .agg(etf_share_total=("shares", "sum"), etf_funds=("fund_code", "nunique"))
        .sort_values("snapshot_date")
    )
    for group, keywords in BENCHMARK_KEYWORDS.items():
        mask = eq["fund_name"].fillna("").map(
            lambda name, kws=keywords: any(k in name for k in kws)
        )
        sub = (
            eq[mask]
            .groupby("snapshot_date", as_index=False)["shares"]
            .sum()
            .rename(columns={"shares": f"etf_share_{group}"})
        )
        agg = agg.merge(sub, on="snapshot_date", how="left")
        agg[f"etf_share_{group}"] = agg[f"etf_share_{group}"].fillna(0.0)

    # 月度变化（当期快照 vs 上一快照，天然只用过去）；缩尾防新发 ETF 造成极端跳变
    for col in [c for c in agg.columns if c.startswith("etf_share_")]:
        agg[f"{col}_chg_1m"] = agg[col].pct_change().clip(-0.5, 0.5)
        agg[f"{col}_chg_3m"] = agg[col].pct_change(3).clip(-0.8, 0.8)
    agg["etf_share_total_z12"] = (
        agg["etf_share_total_chg_1m"]
        - agg["etf_share_total_chg_1m"].rolling(12).mean()
    ) / agg["etf_share_total_chg_1m"].rolling(12).std().replace(0, np.nan)

    # 下一个交易日可用（避免用当日收盘后才知道的快照给当日信号）
    trading = pd.Series(days, index=days)
    next_day = {}
    for snap in agg["snapshot_date"]:
        later = trading[trading > snap]
        next_day[snap] = later.iloc[0] if len(later) else snap + pd.Timedelta(days=1)
    agg["available_date"] = agg["snapshot_date"].map(next_day)
    agg = agg.sort_values("available_date").reset_index(drop=True)
    agg.to_parquet(out, index=False)
    _write_meta(
        ext / "etf_shares_monthly_meta.json",
        {
            "source": "akshare.fund_etf_scale_sse(date=...)",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(agg)),
            "months": int(len(agg)),
            "start": str(agg["snapshot_date"].min().date()),
            "end": str(agg["snapshot_date"].max().date()),
            "failed_months": failed,
            "equity_prefixes": list(EQUITY_ETF_PREFIXES),
            "notes": [
                "仅沪市 ETF 份额有历史接口；深市接口只返回当前快照，故序列为「沪市 ETF」",
                "股票 ETF 按代码前缀过滤，剔除货币/债券(511)、跨境(513)、黄金(518)",
                "available_date=下一交易日，日频合并必须用它，禁止用 snapshot_date",
            ],
        },
    )
    logger.info("ETF 份额月度落盘：%s（%d 个月）", out, len(agg))
    return out


def build_index_futures_basis(
    out_dir: Path = DEFAULT_DIR, *, force: bool = False
) -> Path:
    """股指期货主力连续基差（IF/IH/IC/IM）。

    本地 `futures_daily.parquet` 的主力连续是当日主力合约的真实价格（已验证
    与现货基差在 ±4% 内），不是复权价；换月日会有跳变，用 5 日中位数平滑。
    """
    import duckdb

    from backend.shared.benchmark import load_index_frame

    ext = _external_dir(out_dir)
    out = ext / "index_futures_basis.parquet"
    if out.exists() and not force:
        logger.info("复用 %s", out)
        return out

    fut_path = Path("/data/quantfutures/9_hithink_fuyao/futures_daily.parquet")
    if not fut_path.exists():
        raise FileNotFoundError(f"期货日线不存在：{fut_path}")
    con = duckdb.connect()
    try:
        fut = con.execute(
            """
            SELECT time AS trade_date, variety_code, close
            FROM read_parquet(?)
            WHERE is_continuous AND contract_type = 'main_continuous'
              AND variety_code IN ('IF', 'IH', 'IC', 'IM')
            """,
            [str(fut_path)],
        ).fetchdf()
    finally:
        con.close()
    fut["trade_date"] = pd.to_datetime(fut["trade_date"]).dt.normalize()
    fut = fut.sort_values(["variety_code", "trade_date"])

    spot = load_index_frame(
        columns=("close",),
        symbols=list(INDEX_FUTURES_SPOT.values()),
        start=str(fut["trade_date"].min().date()),
        end=str(fut["trade_date"].max().date()),
        market="CN",
    )
    spot["trade_date"] = pd.to_datetime(spot["time"]).dt.normalize()
    spot = spot.rename(columns={"symbol": "index_code", "close": "spot_close"})
    spot = spot[["trade_date", "index_code", "spot_close"]].drop_duplicates(
        subset=["trade_date", "index_code"]
    )

    rows: list[pd.DataFrame] = []
    for variety, index_code in INDEX_FUTURES_SPOT.items():
        f = fut[fut["variety_code"] == variety][["trade_date", "close"]].rename(
            columns={"close": "fut_close"}
        )
        s = spot[spot["index_code"] == index_code][["trade_date", "spot_close"]]
        merged = f.merge(s, on="trade_date", how="inner").sort_values("trade_date")
        if merged.empty:
            continue
        prefix = variety.lower()
        merged[f"{prefix}_basis"] = merged["fut_close"] / merged["spot_close"] - 1
        for window in (5, 20):
            merged[f"{prefix}_basis_med{window}"] = (
                merged[f"{prefix}_basis"].rolling(window).median()
            )
        merged[f"{prefix}_basis_chg_5"] = merged[f"{prefix}_basis_med5"].diff(5)
        merged[f"{prefix}_basis_z60"] = (
            merged[f"{prefix}_basis_med5"]
            - merged[f"{prefix}_basis_med5"].rolling(60).mean()
        ) / merged[f"{prefix}_basis_med5"].rolling(60).std().replace(0, np.nan)
        rows.append(
            merged[
                [
                    "trade_date",
                    f"{prefix}_basis",
                    f"{prefix}_basis_med5",
                    f"{prefix}_basis_chg_5",
                    f"{prefix}_basis_z60",
                ]
            ]
        )

    if not rows:
        raise RuntimeError("股指期货基差为空（期货/现货日期未匹配）")
    full = rows[0]
    for frame in rows[1:]:
        full = full.merge(frame, on="trade_date", how="outer")
    full = full.sort_values("trade_date").reset_index(drop=True)
    if {"if_basis_med5", "ic_basis_med5"} <= set(full.columns):
        full["basis_if_ic_spread"] = full["if_basis_med5"] - full["ic_basis_med5"]
    if {"if_basis_med5", "im_basis_med5"} <= set(full.columns):
        full["basis_if_im_spread"] = full["if_basis_med5"] - full["im_basis_med5"]

    full.to_parquet(out, index=False)
    _write_meta(
        ext / "index_futures_basis_meta.json",
        {
            "source": "local futures_daily.parquet main_continuous + index_daily spot",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(full)),
            "start": str(full["trade_date"].min().date()),
            "end": str(full["trade_date"].max().date()),
            "mapping": INDEX_FUTURES_SPOT,
            "notes": [
                "basis = 主力连续收盘 / 现货指数收盘 - 1",
                "主力连续换月日有跳变，使用 5 日中位数（_med5）作为主特征",
                "IM 自 2022-07 起有数据，之前为 NaN",
            ],
        },
    )
    logger.info("股指期货基差落盘：%s（%d 行）", out, len(full))
    return out


def pit_membership_asof(
    membership: pd.DataFrame, as_of: pd.Timestamp | str
) -> pd.DataFrame:
    """按 as-of 还原某日个股申万一级行业归属（PIT）。

    每个股票取 `start_date <= as_of` 的最后一条；无记录的股票不返回。
    """
    as_of = pd.Timestamp(as_of).normalize()
    m = membership[membership["start_date"] <= as_of]
    if m.empty:
        return pd.DataFrame(columns=["symbol_prefix", "industry_code", "industry_l1"])
    idx = m.groupby("symbol_prefix")["start_date"].idxmax()
    return m.loc[idx, ["symbol_prefix", "industry_code", "industry_l1"]].reset_index(
        drop=True
    )


def build_all(
    out_dir: Path = DEFAULT_DIR, *, start: str = "2015-01-01", force: bool = False
) -> dict[str, str]:
    return {
        "sw_indices": str(fetch_sw_l1_indices(out_dir, start=start, force=force)),
        "membership": str(fetch_sw_membership_pit(out_dir, force=force)),
        "macro": str(fetch_macro_daily(out_dir, start=start, force=force)),
        "etf": str(fetch_etf_shares_monthly(out_dir, start=start, force=force)),
        "basis": str(build_index_futures_basis(out_dir, force=force)),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--sw-indices", action="store_true")
    parser.add_argument("--membership", action="store_true")
    parser.add_argument("--macro", action="store_true")
    parser.add_argument("--etf", action="store_true")
    parser.add_argument("--basis", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.dir)
    result: dict[str, str] = {}
    if args.all or args.sw_indices:
        result["sw_indices"] = str(
            fetch_sw_l1_indices(out_dir, start=args.start, force=args.force)
        )
    if args.all or args.membership:
        result["membership"] = str(fetch_sw_membership_pit(out_dir, force=args.force))
    if args.all or args.macro:
        result["macro"] = str(fetch_macro_daily(out_dir, start=args.start, force=args.force))
    if args.all or args.etf:
        result["etf"] = str(
            fetch_etf_shares_monthly(out_dir, start=args.start, force=args.force)
        )
    if args.all or args.basis:
        result["basis"] = str(build_index_futures_basis(out_dir, force=args.force))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
