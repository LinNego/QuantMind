# research/ —— 研究工作区

> 这里放**学习过程中的代码和产出**。产品代码在 `backend/`，不要混。
> 对应文档：`docs/量化学习/`（从 [11-协作约定](../docs/量化学习/1-开始/11-协作约定.md) 开始读）

---

## 目录结构

```
research/
├── README.md              本文
├── qdb.sql                ★ 23 张表的 DuckDB 视图，duckdb -init 加载
│
├── stage0/                阶段 0：数据体检（D01–D12）
│   ├── d01_data_check.py      D01 看见数据 + D04 脏数据 + D07 单位
│   ├── d02_coverage.py        D02 规模 + D03 每天股票数
│   ├── d06_missing.py         D06 缺失率与起始日 + D10 两个暗坑
│   ├── inspect_tables.py      23 张表的结构字典
│   ├── practice.py            改数字玩的练习
│   └── full_check.py          ← 参考实现，一次跑完 D01–D10（不要照抄）
│
├── stage1/                阶段 1：第一个策略 + 成本（D13–D27）
│   └── toy_backtest.py        同一信号四种跑法对比（未来函数 / 成本）
│
├── lessons/               工具练习（不属于任何阶段）
│   ├── pandas_drill.py        pandas 6 课，逐行打印
│   ├── pandas_cheatsheet.md   SQL ↔ pandas 速查
│   └── pandas_basics.py       全市场 128 万行走一遍五步
│
├── tools/                 通用工具
│   ├── download_quantdb.py    下载/补数据（断点续传）
│   ├── repl.py                交互式查数据
│   └── requirements-research.txt
│
└── out/                   ★ 所有产出（脚本自动写这里）
    ├── D01_data_check.md
    ├── D02_coverage.md
    ├── D06_missing.md
    ├── tables_schema.md       23 张表结构字典（inspect_tables.py 生成）
    ├── tables_overview.csv
    ├── fig/                   所有图
    └── tbl/                   所有结果表
```

---

## 三条规矩

**① 代码按阶段放，按「学习日」命名**

```
stage0/d06_missing.py     ← D06 的脚本
stage1/toy_backtest.py    ← 阶段 1 的脚本
```

一个脚本只回答一个格子的问题。**不要写"什么都能干"的大脚本。**

**② 产出全部落 `out/`，禁止只 print 不落盘**

```python
OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "D06_missing.md").write_text("\n".join(lines), encoding="utf-8")
```

半年后你要能凭一个日期找回任意一份报告。

**③ 脚本能独立跑**

```bash
qm
python research/stage0/d06_missing.py
```

不依赖外部参数、不依赖先跑别的脚本、不依赖当前目录。

---

## 常用命令

```bash
qm                                  # 进仓库
qd                                  # 进仓库 + 开 DuckDB（23 张表已建视图）
python research/stage0/d06_missing.py
python research/lessons/pandas_drill.py
```

DuckDB 里可用短名（见 `qdb.sql` 末尾）：

| 短名 | 表 |
|---|---|
| `ku` / `kf` / `kb` | 不复权 / 前复权 / 后复权 日线 |
| `feat` | features_daily（79 列宽表） |
| `val` / `ti` / `idx` | 估值 / 技术指标 / 指数 |

---

## 数据在哪

| | 路径 |
|---|---|
| 宿主机 | `/opt/quantmind/data/quantdb`（11 GB，23 张表） |
| 容器内 | `/data/quantdb` |
| 覆盖区间 | 2016-01-04 ～ 2026-10-23 |

脚本统一用 `os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")`，换机器只改环境变量。

---

## 不许进这里的东西

| 别放 | 为什么 |
|---|---|
| 大文件（`*.parquet` / `*.pkl` / `*.h5`） | `.gitignore` 已排除，放了也不生效 |
| 复制粘贴的完整数据集 | 11 GB 不进 git |
| 别人的策略代码 | 抄参数 = 抄过拟合 |
| 一次性的临时探测脚本 | 放 `scratch/`（也已 gitignore） |

---

## 新 Agent 须知

读 [11-协作约定](../docs/量化学习/1-开始/11-协作约定.md)，重点是边界：

- **阶段 0：我只跑命令，AI 只给命令和判读标准，不代跑**
- **阶段 1 起：AI 写代码，我写规格 + 按 5 问验收**
