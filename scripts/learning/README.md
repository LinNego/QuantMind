# 学习工作区：AI Agent × 量化

这里放学习阶段的脚本，**不属于生产代码**，可以随便改、随便删。

> **定位**：这是「实验室」，不是教材。
> 原理与判据在 [research/手册](../../research/手册/README.md)，每章跑什么在
> [实验手册](../../research/手册/实验手册.md)，自测在 [评测题库](../../research/手册/评测题库.md)，
> 16 周节奏在 [Agent 与量化学习路线](../../research/手册/Agent与量化学习路线.md)。

## 文件

| 文件 | 讲什么 | 什么时候看 |
|---|---|---|
| [`00_mini.py`](00_mini.py) | 最小示例：1 个特征、1 条直线、1 个 IC | 第 1 天 |
| [`01_ml_intro.py`](01_ml_intro.py) | 完整版：6 个特征 + 四种情景（诚实/自欺/泄漏/安慰剂） | 第 2–3 天 |
| [`02_visuals.py`](02_visuals.py) | 三张图：拟合线 / 过拟合 / IC 随时间 | 第 3 天 |
| [`03_mindmap.py`](03_mindmap.py) | 生成 16 周路线脑图 | 随时 |
| [`04_correlation.py`](04_correlation.py) | 谁和小盘策略相关性最低（候选第二腿） | 做到 03/04 时 |
| [`05_dividend_vs_smallcap.py`](05_dividend_vs_smallcap.py) | 送转 vs 微盘：相关性 / 年化 / 回撤 / 组合效果 | 做到 03 时 |

事件相关脚本在 [`scripts/events/`](../events/)：

| 文件 | 讲什么 |
|---|---|
| [`01_build_event_library.py`](../events/01_build_event_library.py) | 公告 → 标准事件库（去重、is_first、days_since） |
| [`02_event_study.py`](../events/02_event_study.py) | 最小事件研究：AR、t 值、安慰剂 |

## 运行方式

```bash
cd /root/quant/QuantMind
/root/quant/.venv/bin/python3 scripts/learning/00_mini.py
```

输出图片写到 `docs/images/learning/`。

## 环境

- Python：`/root/quant/.venv/bin/python3`（3.12）
- 已装：numpy、pandas、duckdb、matplotlib、scipy、pyarrow、scikit-learn、lightgbm
- 装包用 `uv`（venv 里没有 pip）：

```bash
/root/.local/bin/uv pip install --python /root/quant/.venv/bin/python <包名>
```

## 数据

全部在 `/opt/quantmind/data/quantdb/`：

| 数据集 | 内容 | 大小 | 状态 |
|---|---|---|---|
| `6_ml_datasets/features_daily` | 79 列日频特征 + 未来收益标签 | 3.4G | ✅ |
| `1_kline_data/daily_unadjusted` | 不复权日线（算真实价格用） | 240M | ✅ |
| `1_kline_data/index_daily` | 指数日线（国证2000 等 46 个） | 21M | ✅ |
| `5_technical_derived` | 技术指标 / 估值 / 情绪 | 4.3G | ✅ |
| `6_ml_datasets/alpha_library` | Alpha101 + GTJA191 + Alpha158 | — | ❌ 未下载 |

**前四个够第 1–8 周用。** `alpha_library` 到做「用标准答案校准评测器」时再下。

事件数据在 `data/events/`（`event_library.parquet`、`event_study_result.parquet`），
由 `scripts/events/01` 和 `02` 生成。

## 记录

每周在 [`LOG.md`](LOG.md) 写一条：**跑出了什么数字、卡在哪、下周第一件事**。
不要写「学习了 XXX」。

> **先预测，再跑。** 跑之前写下「我预计 IC 多少」，跑完对比——这是校准判断力的唯一方法。
