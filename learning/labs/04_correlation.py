#!/usr/bin/env python3
"""测算：谁和小盘策略的相关性最低？

不是猜，是用你的数据算。

做法：
1. 取微盘策略的月度净值（来自月度回测 JSON）
2. 取各指数月度收益
3. 构造三个简单的因子策略（反转 / 动量 / 低波）作为「候选第二腿」
4. 算相关矩阵，看谁最独立
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DATA = Path("/opt/quantmind/data/quantdb")
FEAT = f"{DATA}/6_ml_datasets/features_daily"
IDX = f"{DATA}/1_kline_data/index_daily"

INDEX_NAME = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399303.SZ": "国证2000",
    "000016.SH": "上证50",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
}


def to_month_end(s: pd.Series) -> pd.Series:
    """日频序列 → 月末值，索引统一成「月度周期」，避免对齐问题。"""
    out = s.resample("ME").last()
    out.index = out.index.to_period("M")
    return out


def monthly_returns_from_prices(px: pd.Series) -> pd.Series:
    """价格序列 → 月收益率。"""
    return px.pct_change()


def load_strategy_monthly(json_path: Path) -> pd.Series:
    """微盘策略的月度净值 → 月收益。"""
    d = json.load(open(json_path))
    nav = pd.Series(d["strategy"]["nav"])
    nav.index = pd.to_datetime(nav.index.astype(str), format="%Y%m%d")
    nav = nav.sort_index()
    r = nav.pct_change().dropna()
    # ⚠️ 净值索引是「信号日」，但收益属于信号日之后的那一个月，要 +1 才对得齐
    r.index = r.index.to_period("M") + 1
    return r


def load_index_monthly(con) -> pd.DataFrame:
    """46 个指数 → 月度收益宽表。"""
    df = con.execute(
        f"SELECT symbol, time, close FROM read_parquet('{IDX}/**/*.parquet') "
        "WHERE time >= DATE '2017-01-01'"
    ).df()
    df["time"] = pd.to_datetime(df["time"])
    out = {}
    for sym, g in df.groupby("symbol"):
        px = g.set_index("time").close.sort_index()
        r = monthly_returns_from_prices(to_month_end(px))
        out[sym] = r
    wide = pd.DataFrame(out).sort_index()
    return wide


def factor_strategy(
    con, factor: str, ascending: bool, topk: int = 30, start: str = "2017-01-01"
) -> pd.Series:
    """月末按因子排序选 topk，等权持有下月。

    ascending=True 表示因子值越小越好（反转、低波）；False 表示越大越好（动量）。
    """
    df = con.execute(
        f"""
        SELECT symbol, time, close, {factor} AS f
        FROM read_parquet('{FEAT}/**/*.parquet')
        WHERE time >= DATE '{start}' AND {factor} IS NOT NULL AND close > 1
        """
    ).df()
    df["time"] = pd.to_datetime(df["time"])

    # 月末快照
    df["ym"] = df.time.dt.to_period("M")
    snap = df.sort_values("time").groupby(["ym", "symbol"]).tail(1)

    # 每只股票的月末价格 → 月收益
    px = snap.pivot_table(index="ym", columns="symbol", values="close")
    ret_next = px.pct_change().shift(-1)          # 下个月收益
    fval = snap.pivot_table(index="ym", columns="symbol", values="f")

    out = {}
    for ym in fval.index:
        f_row = fval.loc[ym].dropna()
        if len(f_row) < topk:
            continue
        picked = f_row.sort_values(ascending=ascending).head(topk).index
        if ym not in ret_next.index:
            continue
        r = ret_next.loc[ym].reindex(picked).dropna()
        if len(r):
            out[ym] = float(r.mean())
    idx = pd.PeriodIndex([ym + 1 for ym in out], freq="M")   # 收益属于下一个月的
    return pd.Series(list(out.values()), index=idx)


def main() -> None:
    con = duckdb.connect()

    print("=" * 78)
    print("一、微盘策略的月度收益（来自回测）")
    print("=" * 78)
    strat = load_strategy_monthly(Path("/tmp/sep25_base.json"))
    print(f"{len(strat)} 个月，均值 {strat.mean()*100:.2f}%，"
          f"波动 {strat.std()*np.sqrt(12)*100:.1f}%")

    print("\n" + "=" * 78)
    print("二、指数月度收益（相关性候选）")
    print("=" * 78)
    idx = load_index_monthly(con)
    print(f"共 {idx.shape[1]} 个指数")

    print("\n" + "=" * 78)
    print("三、构造三个「候选第二腿」因子策略")
    print("=" * 78)
    cands = {
        "反转(20日跌幅最大)": factor_strategy(con, "pct_change", ascending=True),
        "动量(20日涨幅最大)": factor_strategy(con, "pct_change", ascending=False),
        "低波(20日波动最小)": factor_strategy(con, "vol_std_20", ascending=True),
    }
    for k, v in cands.items():
        print(f"  {k}: {len(v)} 个月，均值 {v.mean()*100:+.2f}%，"
              f"年化波动 {v.std()*np.sqrt(12)*100:.1f}%")

    # 合并所有候选
    allc = idx.copy()
    for k, v in cands.items():
        allc[k] = v

    print("\n" + "=" * 78)
    print("四、与微盘策略的相关性（越低越好）")
    print("=" * 78)
    common = allc.index.intersection(strat.index)
    print(f"共同区间：{common.min()} ~ {common.max()}，{len(common)} 个月\n")

    corr = allc.loc[common].apply(lambda col: col.corr(strat.loc[common]))
    corr = corr.dropna().sort_values()

    rows = []
    for k, v in corr.items():
        name = INDEX_NAME.get(k, k)
        kind = "指数" if k in idx.columns else "因子策略"
        rows.append({"对象": name, "类型": kind, "与微盘相关性": round(v, 3)})
    out = pd.DataFrame(rows)

    print("—— 相关性最低的 10 个 ——")
    print(out.head(10).to_string(index=False))
    print("\n—— 相关性最高的 5 个 ——")
    print(out.tail(5).to_string(index=False))

    print("\n" + "=" * 78)
    print("五、候选第二腿的独立价值（年化 + 相关性）")
    print("=" * 78)
    for k, v in cands.items():
        c = v.reindex(common).corr(strat.reindex(common))
        ann = (1 + v.mean()) ** 12 - 1
        vol = v.std() * np.sqrt(12)
        print(f"  {k:22} 年化 {ann*100:+6.1f}%  波动 {vol*100:5.1f}%  "
              f"与微盘相关 {c:+.2f}")


if __name__ == "__main__":
    main()
