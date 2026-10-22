# 机构级期货因子库

面向**短周期（1–5 日）预测**的境内期货因子面板。一行 = 一个品种 × 一个交易日，
所有因子只使用当日收盘及以前可得的信息；未来收益标签独立落盘，不进入因子表。

覆盖 84 个境内商品/金融期货主力连续（`*0.CN`），共 111 个因子列，分 13 族。

## 产物

```
<QM_QUANTFUTURES_DATA_DIR>/6_ml_datasets/
├── futures_factors/
│   ├── dt=YYYYMMDD/data.parquet   # 特征：symbol,date,time,dt + OHLCV + 111 因子
│   ├── symbol_meta.parquet        # 品种/板块/交易所/合约乘数/单位
│   └── manifest.json              # 版本、区间、行数、自检报告、数据源
└── futures_factors_labels/
    └── dt=YYYYMMDD/data.parquet   # 标签：fwd_ret_{1,3,5,10,20}d + 截面版本
```

固定列顺序见 `compute.PANEL_COLUMNS`；因子列清单见 `registry.FEATURE_COLS`。

## 因子族

| 族 | 列数 | 经济逻辑 | 代表列 |
|----|-----:|----------|--------|
| price | 19 | 多周期动量、均线偏离、RSI/MACD/布林、突破、日内结构 | `ret_5d` `ma_gap_20` `brk_20` `clv` |
| volatility | 11 | 已实现/帕金森/Garman-Klass 波动、下行波动、回撤、偏度、波动分位 | `rv_20` `park_20` `vol_pct_252` |
| liquidity | 5 | 成交额水平与变化、Amihud 非流动性 | `log_notional` `amihud_20` |
| open_interest | 11 | 总持仓变化、价量仓象限、近月/前三/主力持仓占比、合约数 | `oi_chg_5` `poi_quad` `front_oi_share` |
| term | 11 | 期限结构：carry、斜率、曲率、近远月价差、到期天数、移仓标记 | `carry` `term_slope` `dte_dom` |
| basis | 7 | 主力/近月基差及其变化、期限基差曲线、252 日分位 | `basis_dom` `basis_curve` `basis_z_252` |
| inventory | 8 | 库存水平、变化、分位、动量、库存波动 | `inv_z_20` `inv_pct_252` |
| warehouse | 6 | 仓单数量、变化、分位 | `wh_chg_5` `wh_pct_252` |
| position | 7 | 会员多空净持仓、集中度、HHI | `member_net_ratio` `member_hhi` |
| cross_section | 6 | 同日全品种分位（动量、carry、基差、持仓变化、波动） | `xs_rank_carry` `xs_rank_ret_5` |
| sector | 5 | 板块相对强弱、板块动量、板块 beta、残差动量 | `sec_rel_ret_20` `resid_mom_20` |
| seasonality | 6 | 月份周期、同月历史收益、月末效应、星期效应 | `month_sin` `days_to_month_end` |
| meta | 9 | 可得性标记（0/1），让模型区分「因子为 0」与「当天没这类数据」 | `meta_has_oi` `meta_roll_adjusted` |

期货专属逻辑集中在 `term` / `open_interest` / `basis` / `inventory` / `position`
五族：carry 与基差刻画持有收益，价仓象限区分增仓趋势与减仓趋势，库存/仓单
刻画现货松紧，会员持仓刻画多空拥挤度。价量族与横截面族负责短周期时序与相对强弱。

## 口径

- **移仓复权**：`adj_close_t = adj_close_{t-1} × close(D_t,t) / close(D_t,t-1)`，
  `D_t` 为当日持仓量最大的合约；序列锚定到最新原始收盘价。2022-09 之前没有逐合约
  数据，用首个可用复权乘数拼接，`meta_roll_adjusted=0` 标记这些行。
- **基差**：`basis_dom = 期货 − 现货`，正值 = 期货升水（沿用 QuantFutures 口径）。
- **carry**：年化近远月价差，`carry > 0` = backwardation。
- **term_slope**：持仓量加权 `ln(price)` 对到期天数的斜率，`> 0` = contango。
  两者符号相反是设计使然，不要互相校准。
- **辅助数据滞后**：库存、仓单、会员持仓在收盘后公布，统一 `shift(1)` 个交易日。
- **横截面因子**：`xs_*` 是同日全品种分位（0~1），只用于同日截面模型；
  与 `fwd_xs_*` 标签配套。
- **成交额**：QuantFutures 的 `amount = price × volume`，不含合约乘数；
  名义金额需乘 `symbol_meta.multiplier`。

## 标签

```
fwd_ret_hd = close[t+1+h] / close[t+1] - 1
fwd_xs_hd  = fwd_ret_hd - 同日全品种均值
```

- `execution_lag=1`：信号在 T 日收盘后生成，最早 T+1 收盘成交，
  因此**从 T+1 收盘起算**，避免用 T+1 开盘价制造不可实现的收益。
- 尾部 `h` 天为 NaN 是预期行为，不能填 0。
- 标签目录独立，训练脚本必须显式 join，禁止把 `fwd_*` 扫进特征轴。

## 数据可得性（2026-10-22 实测）

| 因子族 | 起点 | 近年覆盖 | 说明 |
|--------|------|---------:|------|
| price / volatility / liquidity / seasonality / sector | 2005 | ≈100% | 主力连续全历史 |
| open_interest / term | 2025 | 90%+（2026） | `contracts_daily` 在 2022–2024 只有 2–65 个品种，2025 起才到 80 个 |
| basis | 2024-11 | ≈65%（2026） | `spot_basis` 源只覆盖部分品种 |
| inventory | 2025-09 | ≈24%（2026） | `inventory_em` 只覆盖部分品种 |
| warehouse | 2025 | ≈13%（2026） | 交易所仓单只覆盖部分品种 |
| position | 2026-10 | ≈1%（2026） | `member_rank_sina` 目前只有 6 个交易日 |

**训练窗口建议**

- **价量基线模型**：2016-01 起（约 10 年），用 price/volatility/liquidity/
  cross_section/sector/seasonality 族。
- **全因子模型**：2025-01 起，term/OI 才真正可用；样本约 3.4 万行。
- **截面排序模型**：任意窗口，标签用 `fwd_xs_*`，特征优先 `xs_rank_*` 与板块族。

覆盖不足的族不要硬塞进模型：缺失率 >70% 的列在树模型里会退化成「按品种记忆」，
在深度模型里会放大 missingness 噪声。先用 `meta_has_*` 做门控或直接删列。

## 首轮 IC 体检（2026-10-22）

口径：标签用本库 `fwd_ret_hd`（T+1 收盘起算），窗口 2025-01-02 起、
434 个交易日 / 80 个品种；逐日截面 Spearman rank IC，剔除当日样本 <20 或
因子唯一值 <5 的日期；`t = ICIR × √天数`。

| 因子 | 1d IC (t) | 3d IC (t) | 5d IC (t) | 有效天数 |
|------|----------:|----------:|----------:|---------:|
| `carry_3m` | +0.043 (3.8) | +0.062 (5.0) | +0.065 (5.2) | 430 |
| `term_slope` | −0.042 (−3.7) | −0.059 (−4.7) | −0.066 (−5.2) | 430 |
| `basis_curve` | −0.032 (−2.9) | −0.047 (−4.1) | −0.059 (−5.0) | 429 |
| `carry` / `xs_rank_carry` | +0.030 (3.4) | +0.041 (4.3) | +0.048 (4.9) | 430 |
| `near_far_spread` | +0.030 (3.4) | +0.040 (4.2) | +0.046 (4.7) | 430 |
| `front_oi_share` | — | — | +0.036 (3.7) | 428 |
| `ma_gap_60` | +0.030 (2.0) | +0.036 (2.3) | +0.033 (2.2) | 432 |
| `basis_chg_5` | +0.026 (2.5) | +0.034 (3.4) | +0.031 (2.9) | 429 |

- 短周期 alpha 主要来自**期限结构/基差**：`carry_3m` 与 `term_slope` 的 t 值最高，
  且 3–5 日强于 1 日；两者符号相反是口径设计。
- 价量族：1 日以短期反转为主（`ret_1d` 报告 IC ≈ −0.029），5 日动量才转正
  （`ma_gap_60` +0.033）。
- 库存族 5 日 IC 更高（`inv_z_20` +0.062，t=3.6；`inv_chg_20` +0.051），但只有
  44–59 个有效交易日，**只能当候选**，不能据此定权重。
- `member_*` 目前只有 6 个交易日，标准报告里的 IC 0.09–0.13 不可采信，训练先禁用。
- 日历 5 列（`month_sin`/`month_cos`/`days_to_month_end`/`day_of_week`/
  `is_quarter_end`）在每个交易日内是常数，没有截面 IC；只能当时间特征或分域变量，
  不要放进截面排序模型。
- `xs_rank_*` 与其原始因子的 rank IC 完全相同（单调变换不改秩相关），只是给线性
  模型提供可比尺度；树模型二选一即可。
- 高冗余对（同一信息）：`carry`↔`near_far_spread` 0.989、`park_20`↔`gk_20` 0.995、
  `rv_20`↔`park_20` 0.934、`member_net_ratio`↔`member_ls_ratio` 1.000。
  线性/深度模型先每对留一个；树模型可交给正则化。
- 标准报告产物：`report/factor_report.json` + `report/factor_series.parquet`
  （`build_factor_report.py --market FUTURES --dataset futures_factors`）。
  该报告的 `close_fwd` 标签**不含执行滞后**（`close[t+k]/close[t]−1`），
  只用于横向筛查；本节的 IC 才是按训练标签口径算的。

## 训练读取

```python
import os
from pathlib import Path

import pandas as pd

from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.shared.futures_factors.registry import FEATURE_COLS

reader = QuantDBFactorReader(market="FUTURES")
X = reader.read_range(
    "futures_factors", features=list(FEATURE_COLS),
    start="2025-01-01", end="2026-10-21",
)  # 输出 symbol, trade_date, 因子列, OHLCV

root = Path(os.environ.get("QM_QUANTFUTURES_DATA_DIR", "data/quantfutures"))
y = pd.concat(
    pd.read_parquet(p)
    for p in sorted((root / "6_ml_datasets" / "futures_factors_labels").glob("dt=*/data.parquet"))
)
y["trade_date"] = pd.to_datetime(y["date"])
df = X.merge(
    y[["symbol", "trade_date", "fwd_ret_1d", "fwd_xs_1d", "fwd_ret_5d", "fwd_xs_5d"]],
    on=["symbol", "trade_date"], how="left",
)
```

`read_range` 的 `features` 必须显式传列名；`futures_factors_labels` 已在读取层
的发现/训练排除清单里，不会被自动当成因子源。

## 训练前处理

1. **切分**：`fwd_*` 标签是重叠窗口，按时间切分时验证/测试集前留 `h+1` 个交易日
   的 embargo；随机切分会把同一条未来收益同时放进训练和验证。
2. **缺失**：缺失率 >70% 的列删除或用 `meta_has_*` 门控；树模型保留 NaN，
   线性/深度模型用当日截面中位数填充并加缺失指示列。
3. **截面**：同日做 winsorize（1%/99%）与 z-score；排序目标用 `fwd_xs_hd`，
   回归目标用 `fwd_ret_hd`。
4. **可得性**：`meta_roll_adjusted=0` 的行（2022-09 前）复权链是拼接的，
   跨该边界的动量/波动特征不要与 2022 后同权。

## 构建

```bash
# 全量（特征用全历史计算，写盘可切区间）
python -m backend.scripts.build_futures_factor_dataset \
    --data-root data/quantfutures

# 只保留训练区间（特征仍用全历史算，避免滚动窗口开头空值）
python -m backend.scripts.build_futures_factor_dataset \
    --data-root data/quantfutures --start 2016-01-01

# 每日增量（已有分区不重写）
python -m backend.scripts.build_futures_factor_dataset \
    --data-root data/quantfutures --skip-existing

# 单品种调试
python -m backend.scripts.build_futures_factor_dataset \
    --data-root data/quantfutures --varieties CU,AL,RB --no-labels
```

脚本必须用 `python -m` 运行（直接 `python backend/scripts/...` 无法解析
`backend` 包）。全量构建约 10 分钟、峰值内存约 1.5GB。

**写盘语义**：不带 `--skip-existing` 是**整体替换**——本次没有产出的 `dt=` 分区
会被删除（源数据清洗后整日消失时尤其重要），盘上内容与 manifest 始终一致；
`--skip-existing` 是增量追加，不删除任何已有分区。用 `--start/--end` 构建的是
切片数据集，会把区间外的旧分区一并清掉。

## 自检闸门

`manifest.json` 的 `verification` 段记录：

- **硬闸**：主键重复、缺少必需列/因子列 → 构建直接失败；
- **写盘清洗**：`inf/-inf` 一律转 NaN；
- **告警**：OHLC 不自洽、常量列、覆盖率 <50% 的列（不阻断，但训练前必须处理）；
- **源数据清洗**：整行与前一交易日完全相同的记录视为假期填充并剔除
  （2026-10-22 构建剔除 900 行）；越界的高/低点收敛到
  `[min(open,close), max(open,close)]`，缺失高点保持缺失。

## 版本纪律

改动任何因子口径都必须提升 `registry.FEATURE_VERSION`；`feature_hash()` 是
「版本 + 列顺序」的指纹，写入 manifest。训练侧据此判断是否要重跑特征，
不要在不同 `feature_hash` 的产地上比较模型成绩。
