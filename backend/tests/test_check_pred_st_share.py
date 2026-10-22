"""B3 判据工具：top decile 的 ST 占比 ≤ 同截面全市场占比（2026-10-22）。

背景：审计实测预测 top decile 的 ST 占比 8.5%、全市场 4.0%（2.1×）。修完 PIT
ST 口径后必须以**同一截面**复核——拿全市场静态 ST 数当分母会把「当日停牌/无
预测」的票也算进去，口径不可比。

契约：
- 分母 = 当日**有预测分**的股票（模型实际可选的截面），不是全部上市股票；
- top decile 大小 = max(1, round(n × decile))，n 为该日有分数；
- 判据按**汇总占比**（分子分母各自逐日累加）比较，等权平均会被小截面日放大；
- ST 掩码缺失（稀疏未命中）= 非 ST，与 ``st_mask_over`` 同口径。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.scripts.check_pred_st_share import (
    daily_st_shares,
    summarize,
)


def _pred(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trade_date": pd.to_datetime([r[1] for r in rows]),
            "pred": [r[2] for r in rows],
        }
    )


def _st(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"symbol": [p[0] for p in pairs], "time": [p[1] for p in pairs], "is_st": True}
    )


def test_判据通过_ST全在榜尾时top占比为零():
    rows = [(f"60000{i}.SH", "2024-01-02", float(i)) for i in range(10)]
    st = _st([("600000.SH", "20240102")])  # 分数最低的那只

    shares = daily_st_shares(_pred(rows), st, decile=0.1)
    s = summarize(shares)

    assert s["market_st_share"] == pytest.approx(0.1)
    assert s["top_st_share"] == 0.0
    assert s["pass"] is True


def test_判据不通过_ST霸榜时top占比高于全市场():
    rows = [(f"60000{i}.SH", "2024-01-02", float(i)) for i in range(10)]
    st = _st([("600009.SH", "20240102")])  # 分数最高的那只

    s = summarize(daily_st_shares(_pred(rows), st, decile=0.1))

    assert s["top_st_share"] == 1.0
    assert s["market_st_share"] == pytest.approx(0.1)
    assert s["pass"] is False


def test_decile大小至少一只且按四舍五入():
    rows = [(f"60000{i}.SH", "2024-01-02", float(i)) for i in range(7)]
    shares = daily_st_shares(_pred(rows), _st([]), decile=0.1)

    assert shares.loc[pd.Timestamp("2024-01-02"), "top_n"] == 1  # 0.7 → 1


def test_分母是当日有分数的截面而不是全部票():
    """当日只有 2 只有分数、其中 1 只 ST → 全市场占比 50%（不是 1/10）。"""
    rows = [("600000.SH", "2024-01-02", 1.0), ("600001.SH", "2024-01-02", 2.0)]
    st = _st([("600000.SH", "20240102")])

    shares = daily_st_shares(_pred(rows), st, decile=0.5)

    assert shares.loc[pd.Timestamp("2024-01-02"), "market_st"] == 1
    assert shares.loc[pd.Timestamp("2024-01-02"), "n"] == 2


def test_汇总占比按截面累加而不是按日等权():
    """1 只有分的日子与 100 只有分的日子等权会被小数日放大 → 必须按分子分母累加。"""
    day1 = [("600000.SH", "2024-01-02", 1.0)]  # 100% ST
    day2 = [(f"6001{i:02d}.SH", "2024-01-03", float(i)) for i in range(100)]
    st = _st([("600000.SH", "20240102"), ("600100.SH", "20240103")])

    s = summarize(daily_st_shares(_pred(day1 + day2), st, decile=0.5))

    # 按日等权会是 (1.0 + 0.01)/2 = 0.505；按截面累加是 2/101
    assert s["market_st_share"] == pytest.approx(2 / 101)
    assert s["days"] == 2


def test_缺失预测值与重复行不参与():
    rows = [
        ("600000.SH", "2024-01-02", 1.0),
        ("600001.SH", "2024-01-02", float("nan")),
        ("600002.SH", "2024-01-02", 3.0),
        ("600002.SH", "2024-01-02", 3.0),
    ]

    shares = daily_st_shares(_pred(rows), _st([]), decile=0.5)

    assert shares.loc[pd.Timestamp("2024-01-02"), "n"] == 2


def test_前缀式预测代码与后缀式ST表能对上():
    """pred.parquet 用前缀式 SH600000、st_pit 用后缀式 600000.SH——跨层混用
    会静默零匹配（实测把全市场 ST 占比算成 0.0，判据假通过）。"""
    rows = [("SH600000", "2024-01-02", 1.0), ("SH600001", "2024-01-02", 2.0)]
    st = _st([("600000.SH", "20240102")])

    shares = daily_st_shares(_pred(rows), st, decile=0.5)

    assert shares.loc[pd.Timestamp("2024-01-02"), "market_st"] == 1


def test_空输入返回空表而非抛错():
    out = daily_st_shares(_pred([]), _st([]))
    assert out.empty
    assert summarize(out)["pass"] is False
