# 长窗 vs 冻窗：CatBoost / NativeTFT T+10 对比训练设计（2026-10-21）

> 状态：**已备妥，未提交**。宿主内存不足（见文末「内存前提」），2026-10-21 用户决定暂缓。

## 要回答的问题

把训练窗口从 2023-06 拉到 2025-12、验证与测试都放进 2026，同一套 300 特征、
同一套超参，新模型在 2026 年样本外会不会更好？

一次只改一件事：**只改切分**，特征集合与逐库目录版本、模型超参、标签口径全部与
10/18 冻窗批对齐。否则赢了也说不清是窗口的功劳。

## 两个训练对象的唯一定义

- 特征（300 条）：取自 `data/training_jobs/train_20261018174314_be41e1bf/config.yaml`
  的 `data.features` + `factor_field_sources`（11 个因子库，锚库 `l1_factors`，
  其中 5 条锚库裸名：`concept_exposure_top1`、`style_idio_vol_20`、
  `amt_high_days_10`、`style_idio_vol_60`、`ind_dispersion_20`）。
  **不要**用 `data/quantdb/factor_research/screening/training_selection_300.json`
  的 `features_payload`——那是另一套 300 条（与 10/18 实跑仅 5 条重合）。
- 逐库目录版本：提交时用 `--pin-versions-from` 钉扎到上述基准 config，
  版本不一致脚本直接拒绝提交，保证两批吃同一份因子快照。
- 切分：

  | 段 | 区间 | 说明 |
  |---|---|---|
  | train | 2016-01-04 .. 2025-12-31 | 比冻窗多 2.5 年 |
  | valid | 2026-01-05 .. 2026-04-30 | 全部在 2026 |
  | test | 2026-05-06 .. 2026-09-10 | 全部在 2026 |

  训练器 embargo = `horizon(10) + lag(1) = 11` 个交易日：train 实际末尾约
  2025-12-16、valid 实际末尾约 2026-04-中旬；test 不 embargo。
- 超参：CatBoost `num_boost_round=2000 / early_stopping_rounds=100`；
  NativeTFT `dl_params={n_epochs:8, step_len:20, batch_size:4000,
  early_stopping_rounds:5}`；两者 `max_time_minutes=720`，与 10/18 批一致。
- 对照：同 300 特征、同超参的冻窗模型
  （train 2016-01-04..2023-06-18 / valid 2023-06-29..2025-01-21 /
  test 2025-02-01..2026-08-28）。

## 预登记的比较口径（训练前写死，避免事后挑指标）

- **干净共同样本外窗口：2026-05-06 .. 2026-10-16**。
  - 新模型：晚于其 valid 末尾（2026-04-30），干净；
  - 冻窗模型：晚于其 valid 末尾（2025-01-21），同样干净。
  - 冻窗模型的 2025-02..2026-04 对新模型是**样本内**，不得纳入比较。
- 主指标（三项一起看）：
  1. 日频截面 rank-IC 均值与 ICIR（同池、同收益口径）；
  2. Top20 次日命中率超额（`model_next_day_hit.py`：同池基准、可执行口径）；
  3. 10 日策略（`rolling_portfolio_sim.py`：Top20/Top50、S≥20 日均值入场、
     S<均值-0.03 退出、单边 0.1%、1% ADV 上限），从 2026-05-06 起跑。
- 判定纪律：新模型要在**至少两项**上不劣于冻窗（rank-IC 不低于 0.005 以上、
  10 日年化不低于 3pp），才说「拉长窗口更好」；只赢一项按噪声处理，
  不以单个窗口的排名做结论。

## 提交方式

选型 JSON（300 特征 + 新切分）：
`data/quantdb/factor_research/screening/training_selection_300_ext2026.json`

```bash
# CatBoost（先跑）
INTERNAL_CALL_SECRET=$(grep '^INTERNAL_CALL_SECRET=' .env | cut -d= -f2-) \
python3 backend/scripts/submit_crosslib_training.py \
  --selection-json data/quantdb/factor_research/screening/training_selection_300_ext2026.json \
  --model-type catboost \
  --pin-versions-from data/training_jobs/train_20261018174314_be41e1bf/config.yaml \
  --internal-call --internal-user-id 10000001

# NativeTFT（必须等 CatBoost 结束；训练单飞锁并发提交会 409）
INTERNAL_CALL_SECRET=$(grep '^INTERNAL_CALL_SECRET=' .env | cut -d= -f2-) \
python3 backend/scripts/submit_crosslib_training.py \
  --selection-json data/quantdb/factor_research/screening/training_selection_300_ext2026.json \
  --model-type nativetft \
  --pin-versions-from data/training_jobs/train_20261018174314_be41e1bf/config.yaml \
  --internal-call --internal-user-id 10000001
```

- `--internal-call` 是 2026-10-21 新增分支：secret 只从 env `INTERNAL_CALL_SECRET`
  读取（fail-closed），身份头 `X-Internal-Call / X-User-Id / X-Tenant-Id`；
  本机 `QM_PASSWORD` 未配置时用这条路径。
- 提交后脚本会回读 `data/training_jobs/<run_id>/config.yaml` 并断言：
  300 特征逐条来源一致、T+10、method 为空、窗口与 11 库版本逐字一致。
- 两个 run 的 `job_name` 分别为 `T10_Cross300_CAT_Ext2026` 与
  `T10_Cross300_TFT_Ext2026`（display 名带「长窗2026验证」）。

## 选型 JSON 的重建口径

全部 300 条的 catalog key 与 source_column 同名，因此可由基准 config 直接重建：

- `factor_field_sources[name] == "lib:col"` → payload 项 `lib:col`；
- 无冒号（锚库裸名） → payload 项即 `name`；
- `libraries_used` = 冒号左侧的库集合 ∪ {锚库}；`funnel.final = 300`。

## 内存前提（本次暂缓的原因）

- 同类 300 特征训练实测进程峰值 **~24GB**（2026-10-19 日志：读全量因子
  peak 22.3G、切分后 peak 24.0G）；本次训练段多 2.5 年，只会更高。
- 宿主机 2026-10-21 02:00 实测 `MemAvailable` **9.4GB**，
  `SUnreclaim` 27.8GB（CIFS `cifs_small_rq` 泄漏 ~25GB，`drop_caches` 无效）。
- 恢复路径：重启宿主（`scripts/boot_guard.sh` 会拉起容器），或 root 卸载未用
  NAS 共享回收 slab；确认 `MemAvailable > 32GB` 后再提交。
- 远程 `autodl-1` 节点当前离线（SSH 被拒），不可用。
