# QuantDB 增量更新（API Key）

> 结论先说：**你以前每周「全量下载」，是因为用错了入口。**
> `research/tools/download_quantdb.py` 走的是 ModelScope 文件清单，按「整张表」跳过，没有分区和 release 概念；
> 真正的增量走 QuantDB API：`backend/scripts/quantdb_daily_sync.py` + `QUANTDB_API_KEY`。

---

## 0 两条链路，别再混用

| | ModelScope 全量 | QuantDB API 增量 |
|---|---|---|
| 入口 | `research/tools/download_quantdb.py` | `backend/scripts/quantdb_daily_sync.py` |
| 鉴权 | 无 | `X-API-Key`（`QUANTDB_API_KEY`） |
| 粒度 | **整张表**（已有就跳过） | **按交易日分区**（`dt=YYYYMMDD`） |
| 每周更新 | 只能整表重下，或什么都不下 | 只下新分区 / 变更对象 |
| 流量 | 全量 | 增量；`304` 不计费 |
| 状态 | 无 | `~/.quantdb_state/*.sqlite`（release 游标 + 指纹） |
| 适合 | **首次、换机、灾备** | **每日 / 每周补数** |

> 一句话：**ModelScope 负责「从 0 到 1」，API 负责「从 1 到 N」。**

---

## 1 三步配好 API Key

### 1.1 在 QuantDB 创建 Key

登录 <https://www.quantdb.cn> → 个人中心 / 控制台 → **API Keys** → 创建。
明文**只显示一次**，格式 `qdb_...`。

也可以走 API（需要 JWT）：

```bash
curl -X POST https://www.quantdb.cn/api/v1/auth/api-keys \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"description": "QuantMind 增量同步"}'
```

免费用户限 1 个 Key；订阅用户按套餐上限。

### 1.2 写进 QuantMind（三种方式，任选）

**方式 A：管理后台（推荐）**

管理后台 → 数据平台 → **QuantDB** → 「API Key 配置」卡片 → 粘贴 → 保存。
后端会写 `config/runtime.env` 并**当场验证一次**（`/api/v1/auth/me`）。

对应接口：

```bash
POST /api/v1/admin/data-platform/quantdb/config
{"api_key": "qdb_..."}
```

**方式 B：手工写文件**

```bash
echo 'QUANTDB_API_KEY=qdb_你的key' >> config/runtime.env
chmod 600 config/runtime.env
```

**方式 C：环境变量**

```bash
export QUANTDB_API_KEY=qdb_你的key
```

**优先级**：真实环境变量（非空） > `config/runtime.env` > 默认值。
`get_secret()` 每次调用实时重读，管理台换 Key 后**不用重启**（Celery 下一次任务就用新 Key）。

### 1.3 验证

```bash
docker exec quantmind python -c "
from backend.shared.runtime_secrets import get_secret
print('key:', bool(get_secret('QUANTDB_API_KEY')))"
```

或：

```bash
curl https://www.quantdb.cn/api/v1/auth/me -H "X-API-Key: qdb_..."
```

---

## 2 增量同步怎么跑

### 2.1 推荐：容器内跑（SDK 已装）

```bash
# 看状态（不需要 Key，也不下载）
docker exec quantmind python /app/backend/scripts/quantdb_daily_sync.py --status

# 只同步 parquet（最常用；不碰 PG / Qlib）
docker exec quantmind python /app/backend/scripts/quantdb_daily_sync.py --parquet-only

# 只补某几张表
docker exec quantmind python /app/backend/scripts/quantdb_daily_sync.py \
  --parquet-only --datasets daily_forward,valuation,features_daily

# 全链路：parquet → PG → Qlib 缓存 → 特征快照
docker exec quantmind python /app/backend/scripts/quantdb_daily_sync.py

# 只看计划，不下载
docker exec quantmind python /app/backend/scripts/quantdb_daily_sync.py --dry-run
```

### 2.2 宿主机跑：先装 SDK

宿主 venv 默认没有 `quantdb_sdk`（会 `ModuleNotFoundError`）：

```bash
/root/.local/bin/uv pip install --python /root/quant/.venv/bin/python quantdb-sdk==0.5.0
```

然后：

```bash
QM_QUANTDB_DATA_DIR=/opt/quantmind/data/quantdb \
/root/quant/.venv/bin/python3 backend/scripts/quantdb_daily_sync.py --parquet-only
```

### 2.3 关键参数（都在 `--help` 里）

| 参数 | 作用 | 什么时候用 |
|---|---|---|
| `--status` | 各数据集文件数、Qlib/PG 状态 | 每次同步前后 |
| `--dry-run` | 只检查不下载 | 换 Key / 换目录后 |
| `--parquet-only` | 只更 parquet | **90% 的场景** |
| `--datasets a,b` | 只同步指定表 | 补单表 |
| `--since YYYY-MM-DD` | 只同步该日之后的分区 | 近 N 年裁剪 |
| `--reseed-state` | 用本地文件重建状态库 | **换数据目录后必须先跑** |
| `--repair` | 扫描并删掉残缺分区 | 怀疑下载中断 |
| `--full` | 全量重灌 PG（2016 起） | 重建，不是日常 |

> **最容易踩的坑**：把数据目录挪了位置（或从 ModelScope 全量包切到 API），
> 状态库里没有记录 → SDK 认为本地没有 → **全量重下**。
> 先 `--reseed-state`，再增量。

---

## 3 它凭什么只下增量

`quantdb_daily_sync.py` 对每个 V2 数据集做四步：

1. **releases 增量**
   `GET /api/v1/data/releases?datasets=<ds>&after_release=<cursor>`
   返回 release 列表，每个 release 带 `objects[]`（`key` / `sha256` / `size`）。
   只处理新的 `dt=YYYYMMDD` 分区，`/patches/` 直接跳过（补丁已吸收进日分区）。

2. **manifest 补漏**
   服务端可能「对象已更新、release 还没发」。再查一次
   `GET /api/v1/data/download/manifest`，按 `size` 对账，把漏的补上。

3. **指纹对账**
   本地状态库记录每个对象的 `sha256`/ETag。一致就跳过；
   不一致才重下（上游会重算历史分区，比如复权价回溯、因子加列）。

4. **304 免流量**
   下载前带 `If-None-Match`，服务端命中返回 `304`，不传正文、不扣流量。

状态库在 `~/.quantdb_state/`（容器内已挂卷），文件名由 `save_dir` 路径哈希生成——
**所以换目录会让状态失配**。

---

## 4 流量与费用

| 项 | 规则 |
|---|---|
| 计费 | 只对**实际传输**的字节计费 |
| 304 | 不计费 |
| 预览 / 元数据 / manifest | 不计费 |
| 免费用户 | 1GB 一次性体验流量；**第 6 类（模型训练数据集）需订阅** |
| 订阅 | 50GB/月，超出 ¥1/GB |

**估算**：日频 12 个 V2 数据集，每天新增约 12 个分区；正常日更几十 MB 级。
首次全量约 10GB（L0 口径）。所以「每天增量」远比「每周全量」省。

---

## 5 验证：文件数 ≠ 最新日期

`--status` 只给文件数。真正的完整性看**最新分区日期**：

```sql
-- duckdb
SELECT MAX(time) FROM read_parquet('/opt/quantmind/data/quantdb/1_kline_data/daily_forward/*/*.parquet');
```

当前本机（2026-11-15）实测：**数据停在 2026-10-30**，落后约 11 个交易日。

同步后再查一次，应该到最近交易日（或 T-1，取决于上游发布节奏）。

### 常见错误

| 码 | 含义 | 处理 |
|---|---|---|
| 401 | Key 无效 / 过期 | 重新配置 |
| 402 | 余额 / 流量不足 | 充值或买流量包 |
| 403 | 免费用户访问第 6 类 | 订阅，或跳过 `features_daily` |
| 404 | 对象不存在 | 换 `--datasets`，或先 `--repair` |
| 429 | 限流 | 降低并发，稍后重试 |

---

## 6 定时任务

**方式 A：系统 cron（最简单）**

```cron
# 工作日 18:30 增量同步 parquet
30 18 * * 1-5 cd /root/quant/QuantMind && docker exec quantmind \
  python /app/backend/scripts/quantdb_daily_sync.py --parquet-only \
  >> /var/log/quantdb_sync.log 2>&1
```

**方式 B：QuantMind 管理台「同步调度」**
按市场配置时间，写 Redis `quantmind:sync_schedule:{market}`，由后端触发。
（见 `backend/services/engine/README.md` →「定时调度与市场数据同步」）

---

## 7 决策表

| 场景 | 用哪条 |
|---|---|
| 新机器 / 空目录 | ModelScope 全量（`download_quantdb.py`） |
| 每日 / 每周补数 | API 增量（`quantdb_daily_sync.py --parquet-only`） |
| 只补某几张表 | `--datasets` |
| 换数据目录 | 先 `--reseed-state`，再增量 |
| 下载中断 / 文件损坏 | `--repair` |
| 怀疑数据错乱、要重建 | `--full`（重建 PG），parquet 仍走增量 |

---

## 8 一句话

> **首次用 ModelScope，日常用 API；增量靠 release 游标 + 指纹对账，不是靠「再下一次全量」。**
