-- upgrade_v1.1.7: OSS 存量库结构补齐（**仅增量**，无 UPDATE/DELETE/DROP）
--
-- 背景（2026-10-23）：next 与 gitee/master 分叉期间，master 的
--   upgrade_v1.0.9（pool_id）/ v1.0.12（is_favorite）/ v1.0.13（explanation）/
--   v1.0.15（用户因子数据集表）以及 db_init 的若干增量列未进入 next 的升级链，
--   本机库因此缺列缺表。本文件把这些**纯结构增量**收敛成一条可重放脚本。
--
-- 纪律：
--   1. 只允许 ADD COLUMN / CREATE TABLE / CREATE INDEX（全部 IF NOT EXISTS）；
--   2. 不写 UPDATE / DELETE / INSERT / DROP —— 存量数据零影响；
--   3. 启动期 _ensure_upgrade_scripts 会自动重放，必须幂等；
--   4. master 的 v1.0.11/v1.0.14（股票代码后缀化 DELETE）与 db_init 里的
--      DROP TABLE / 状态归一 / FK sweep **不在本文件**，它们会删改存量数据，
--      只能由运维人工评估后单独执行。
-- ============================================================

BEGIN;

SET LOCAL lock_timeout = '10s';

-- 特征字典：存量库补 explanation（db_init 的 CREATE TABLE IF NOT EXISTS 不会补列）
ALTER TABLE qm_feature_definition
    ADD COLUMN IF NOT EXISTS explanation TEXT DEFAULT '';

-- 个人策略收藏星标（与社区 like_count 无关）
ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE;

-- 股票池推理与全市场推理同天并存：pool_id 物理列 + 去重索引
ALTER TABLE qm_model_inference_runs
    ADD COLUMN IF NOT EXISTS pool_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_pool
    ON qm_model_inference_runs (tenant_id, user_id, model_id, pool_id, data_trade_date DESC);

-- 新闻富化 / 全局股票池 v2 的存量库补列（新库由 db_init 自带）
ALTER TABLE news_article_enrichment
    ADD COLUMN IF NOT EXISTS title TEXT;

ALTER TABLE qm_stock_pool
    ADD COLUMN IF NOT EXISTS file_path TEXT;

-- 预检快照唯一约束（代码用 ON CONFLICT ON CONSTRAINT 依赖它；缺失会刷屏报错）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_preflight_snapshot_daily'
    ) THEN
        ALTER TABLE real_trading_preflight_snapshots
            ADD CONSTRAINT uq_preflight_snapshot_daily
            UNIQUE (tenant_id, user_id, trading_mode, snapshot_date);
    END IF;
END $$;

-- 用户因子训练数据集自动发布（generation + active 指针）
CREATE TABLE IF NOT EXISTS public.qm_user_factor_generations (
    generation_id     TEXT PRIMARY KEY,
    dataset_key       TEXT NOT NULL DEFAULT 'l1_factors',
    status            TEXT NOT NULL DEFAULT 'building',
    feature_key       TEXT NOT NULL,
    factor_name       TEXT NOT NULL DEFAULT '',
    expression_sha256 TEXT NOT NULL DEFAULT '',
    manifest_json     TEXT NOT NULL DEFAULT '{}',
    row_count         INTEGER NOT NULL DEFAULT 0,
    byte_count        INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT NOT NULL DEFAULT '',
    created_by        TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at      TIMESTAMPTZ,
    CHECK (dataset_key IN ('l1_factors', 'rd_mined')),
    CHECK (status IN ('building', 'published', 'failed', 'archived'))
);

CREATE TABLE IF NOT EXISTS public.qm_user_factor_dataset_active (
    dataset_key     TEXT PRIMARY KEY,
    generation_id   TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (dataset_key IN ('l1_factors', 'rd_mined'))
);

CREATE INDEX IF NOT EXISTS idx_qm_user_factor_generations_ds_status
    ON public.qm_user_factor_generations (dataset_key, status, created_at DESC);

COMMIT;
