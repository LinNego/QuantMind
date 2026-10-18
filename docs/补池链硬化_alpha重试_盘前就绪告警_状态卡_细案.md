# 补池链硬化：alpha 跳过后自动重试 + 盘前池就绪告警 + 池就绪状态卡

> 2026-10-13 立。目标：让「A 股候选池每天定时产出」这条链**坏了当天能自愈、没自愈住也能在开盘前被喊醒**。
> 本文是动手前的细案；实现在其后。
>
> **2026-10-18 更新**：本文的排程（`2-6`、两档重试）与「alpha 最新即退出」判据已被
> [`docs/补池链盘前双保险_实施细案_20261018.md`](补池链盘前双保险_实施细案_20261018.md) 取代：
> ① 2026-10-16/17 两晚 jq110 在 8G celery worker 被 OOM，形态是 **alpha 最新、CUSTOM 滞后**，
> 旧判据会早退；② `2-6` 漏掉北京周一盘前（JST 与北京同日，仅差 1 小时），已改 `1-5` 并加 07:30 末档。
> 本文保留事故背景与五段链判据，排程与触发条件以新细案为准。

---

## 一、被修的确切故障（2026-10-13 实锤，非推测）

10-13（周二）候选池 `data/reports/stock_picks/20261013_picks.json` **不存在**，决策轮 09:35 起
`rows=0` → `l2.pool_not_member` 拦下全部开仓。四段断链，逐段有证据：

| 段 | 应有产物 | 实际 | 证据 |
|---|---|---|---|
| ① alpha_library | dt=20261012 | **dt=20261009** | `data/quantdb/6_ml_datasets/alpha_library/cron_run.log`：`2026-10-13 02:30:01 跳过：可用内存 14GB < 30GB（alpha=20261009 kline=20261012）` |
| ② CUSTOM l1_factors | dt=20261012 | **dt=20261009** | celery worker `2026-10-13 03:00:00,209 ERROR [SyncSchedule] CUSTOM 数据集重建失败: 源库分区缺失…dt=20261012 缺 alpha_library` |
| ③ 推理批次 | data=2026-10-12 completed | **一次都没有** | 6 条 dispatch 全 `failed`，`reason_detail='主模型维度门禁未通过: QuantDB l1_factors 最新可用 2026-10-09，请求 2026-10-12 超出数据范围'` |
| ④ 候选池 | 20261013_picks.json | **缺** | `ls data/reports/stock_picks/` 无该文件；`postmarket_pipeline.log` 04:00/05:00 两跑均 `无 2026-10-12 的已完成推理批次` |

**①是唯一根因**：内存闸门把 alpha 刷新跳了，②③④ 全是它的下游（②拒写、③维度门禁、④无批次，都是**正确**的 fail-closed 行为）。

`alpha_library_refresh.sh` 是幂等的（flock / 已最新 / 可写 / 内存四闸），**盲跑无害**——这就是「重试」可行的前提。

补充事实（决定重试时刻）：

- alpha 全量重算实测 **43–53 分钟**（`cron_run.log` 历次 `开始刷新`→`结束 rc=0`），峰值 RSS ~24GB。
- 日K `daily_forward` dt=20261012 于 **01:55 JST** 落盘；L2 因子 **02:22 JST** 落盘。
- CUSTOM 增量重建重构代价：`2026-10-12 03:00` 那次只写 0 分区、**0.09 秒**（已最新）；真正补 1 个分区也是秒级。
- 推理脚本单次 ~5–8 分钟（`08:00→08:05→08:11→08:17` 四家依次）。
- ⇒ **自愈窗口充足**：05:30 起跑 alpha → 06:20 完 → CUSTOM 秒级 → 推理 ~6 分钟 → 池 ~06:30 落地，**远早于 09:30 开盘**。

---

## 二、逐项设计

### 项 1：alpha 跳过后自动重试（`scripts/alpha_refresh_retry.sh`，新）

**为什么不改内存闸门**：峰值 24GB / swap 15GB 已满，与实盘抢内存的代价高于「池晚一天」。闸门要留。
**为什么单靠多排几条 cron 不够**：02:30 之后任何一轮 alpha 都要 45 分钟，而 CUSTOM 的定时派发
`03:00 CST` **已经过去**（`quantmind:sync_schedule_last_run:CUSTOM:{date}` 已置位，`market-sync-dispatch`
不会再派）。所以重试必须**自己把下游接上**，否则只是把 alpha 补新、CUSTOM 照样停在旧分区。

新脚本职责（宿主侧，`nice` 低优先级，全程 flock 防重入）：

1. 读分区算 `AL_DT` / `KL_DT`（**不依赖 Redis**，宿主没装 redis-py）。
2. `AL_DT >= KL_DT` → 静默 `exit 0`（链是好的，这是绝大多数日子）。
3. 否则：串行调 `scripts/alpha_library_refresh.sh`（它自带全部闸门与幂等）→ 等它跑完。
4. 跑完**重新实测** `AL_DT`：仍 `< KL_DT`（内存还是不够 / 上游还没落）→ 记日志 `exit 0`，**不猜也不硬来**。
5. 补新成功 → **接下游**（只做这两步，其余交给已有幂等链路）：
   - CUSTOM 重建：容器内 `run_market_sync('CUSTOM', cfg)`（复用唯一实现，含三库增量 + 增量守卫 +
     fail-closed 源库预检）。`cfg.enabled=false` 时**尊重用户设置，不代跑**。
   - 备选池：宿主 `postmarket_pipeline.py --picks-only`（恒重算幂等；缺推理批次时它自己会
     `docker exec trigger_inference.py` 补推理——已实测存在这条路径）。
6. 每步结果写 `logs/keepers/alpha_refresh_retry.log`；有动作时推一条 QQ（自愈成功=🟢，仍失败=🟠）。

cron（宿主 JST，`2-6`）：
```
30 5 * * 2-6  scripts/alpha_refresh_retry.sh
30 6 * * 2-6  scripts/alpha_refresh_retry.sh
```
两档都**收在盘前**（06:30+80min≈07:50，池 08:00 前落地）。常规日第 1 档即 `exit 0`，第 2 档同样空转，
代价是两次 `ls`。

> 不选 03:30/04:30：与 `postmarket_pipeline 04:00` / 容器 08:00 盘前推理撞内存、撞 PG 连接，
> 且那时内存往往还没释放（本次故障正是如此）。05:30 起跑才同时满足「内存已松」与「08:00 前收工」。

### 项 2：盘前池就绪 QQ 告警（`scripts/preopen_pool_check.py`，新）

宿主 cron `50 8 * * 2-6`（**JST 08:50 = 北京 07:50**，开盘前 1 小时 40 分；晚于 06:30 那档重试收工，
早于容器 08:00 盘前推理——所以它看的是「重试后的**终局**」）。

判据（三态，缺一不可）：

| 情形 | 判定 | 动作 |
|---|---|---|
| 池文件**不在** | 🔴 链断了 | **推 QQ** + 附断点诊断 |
| 在，`candidates` 为空，`gate` 含「看空」 | 方向闸**按设计**清空 | **不推**（仅日志） |
| 在，有候选 | 正常 | 仅日志 |

> 第二行是必须区分的：10-12 复盘判「强烈看空（-4.8/11）」，健康链跑出来也是**文件在、0 候选**。
> 把它当故障推会天天误报，运维就会开始忽略告警。判据复用 `decision_round_alerts._BEARISH_MARKER`
> 的同一套「看空」子串口径。

池文件路径**不自己拼**：走 `backend.shared.decision_context_source.pool_path(day)`（已实测宿主可导入），
两代命名（`_agent_picks.json` / `_picks.json`）与报告根口径由它一处决定，避免又一处会漂移的副本。

目标交易日 = `今天`（交易日）或 `next_trading_day(今天)`，走 `scripts/trading_cal.py`。

告警正文用**展示面位置词**（不写「前 20%」），不含任何内部工单号；同一天只推一次（状态文件去重）。
`--dry-run` / `--force` 供人手动摸。

### 项 3：池就绪状态卡

**共享判据 `backend/shared/pool_readiness.py`（新）**——后端接口与宿主脚本**共用一份**「断在哪一段」的实现：

```
assess(*, target, kline_dt, alpha_dt, custom_dt, inference_dt, pool) -> Readiness   # 纯函数，零 IO
collect(*, data_root=None, custom_root=None, reports_dir=None, inference_probe=None) -> Readiness
```

五段串联，`blocked_stage` = **第一个**不就绪的段：

| 段 | 就绪判据 |
|---|---|
| `kline` | `daily_forward` 有分区（这决定「今天该有哪个 data_date 的池」） |
| `alpha_library` | `alpha_library` 最新 dt ≥ `kline` dt |
| `custom_dataset` | `quantcustom/6_ml_datasets/l1_factors` 最新 dt ≥ `kline` dt |
| `inference` | PG 有 `data_trade_date = kline_dt AND status='completed'` 的批 |
| `pool` | `pool_path(target)` 存在且行数 > 0（方向闸清空 → 标 `by_design`，不算 blocked） |

**后端**：`GET /api/v1/admin/data-platform/pool-readiness?market=A`（`data_platform.py`，与 `/freshness`
同风格 `{success,data}` 信封 + `require_admin`）。PG 探针用该文件已有的 `_db_engine()`。

**前端**：`electron/src/features/admin/components/data-management/PoolReadinessCard.tsx`（新），
挂在数据管理页 `AdminQuantDBPanel` 同步调度面板**上方**（一行看清「今天有没有池、断在哪」），
经 `dataPlatformService.getPoolReadiness()` 取数。改完 `npm run typecheck`。

---

## 三、纪律与验收

- **测试先行**（用户 TDD 规则）：`backend/tests/test_pool_readiness.py`（assess 五段/边界/by_design）、
  `scripts/tests/test_preopen_pool_check.py`（三态判据 + 去重 + 目标日解析）、
  `backend/tests/test_pool_readiness_endpoint.py`（信封 + 段序 + 参数）。新建前查重（已确认三者不存在）。
- **不碰**工作区噪声（`.npmrc`、`data/*.sql` 删除、`probe*.js`、`dbg_*.mjs` 等）；`git add` 一律显式 pathspec。
- 部署（cron 落机）**前先向用户确认 SSH 目标与项目目录**。
- 推送需用户显式授权，且只推 gitee + quantmind2/next。
- 本次**不**做：CUSTOM 重建加锁（race 窗口已论证不存在）、1456 因子的不完全 beta 修正、
  `alpha_library_factors.py` 的 true-incremental（上游职责）。
