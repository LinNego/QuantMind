"""信号分快照口径的单测（**唯一实现**共享模块）。

口径漂移的代价：自选页显示 +0.12、哨兵按 -0.03 报警 —— 用户没法信任任何一边。
所以这里盯死三件事：键形归一（裸 6 位 ↔ prefix）、实时/日频如实标注、
分数为 NULL 时**保留 None**（「没有分数」和「分数是 0」不是一回事）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from backend.shared.signal_scores import (
    A_SHARE_MARKET_SQL,
    MIN_SIGNAL_COVERAGE,
    SQL_LATEST_ANY_DATE,
    SQL_LATEST_COVERED_DATE,
    SQL_SCORES_BY_DATE,
    SYMBOL_FOLD_KEY,
    build_score_map,
    normalize_a_share_symbol,
    score_freq_of,
)
from backend.shared.signal_buckets import resolve_feature_version


class TestNormalizeAShareSymbol:
    def test_bare_six_digits_gain_market_prefix(self):
        # engine_signal_scores.symbol 是裸 6 位；持仓/自选侧是 prefix
        assert normalize_a_share_symbol("600036") == "SH600036"
        assert normalize_a_share_symbol("000001") == "SZ000001"
        assert normalize_a_share_symbol("300750") == "SZ300750"
        assert normalize_a_share_symbol("430047") == "BJ430047"

    def test_suffix_form_converts_to_prefix(self):
        assert normalize_a_share_symbol("600036.SH") == "SH600036"

    def test_lowercase_prefix_upper_cases(self):
        assert normalize_a_share_symbol("sh600036") == "SH600036"

    def test_prefix_passes_through(self):
        assert normalize_a_share_symbol("SH600036") == "SH600036"

    def test_non_a_share_is_none(self):
        # 港股/美股/空值一律 None —— 调用方按「这行不参与」处理，不猜市场
        assert normalize_a_share_symbol("AAPL") is None
        assert normalize_a_share_symbol("00700.HK") is None
        assert normalize_a_share_symbol("") is None
        assert normalize_a_share_symbol(None) is None


class TestBuildScoreMap:
    def test_maps_rows_to_prefix_keyed_entries(self):
        rows = [("600036", 0.31, "BUY", "batch", "2026-09-19", 0.882)]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map == {
            "SH600036": {
                "value": 0.31,
                "side": "BUY",
                "freq": "daily",
                "asOf": "2026-09-19",
                "rank_pct": 0.882,
                # 6 列老行没有模型来源列：键在、值 None（「不知道」不是「没有」）
                "bucket": None,
                "model_id": None,
                "model_bucket": None,
            }
        }
        assert realtime_rows == 0

    def test_rank_pct_is_additive_with_none_for_old_rows(self):
        # rank_pct 是 additive 契约列：老行（4/5 列）与 NULL 值都 → 键在值为 None
        # （「没分位」与「分位是 0」不是一回事——0 是这批里垫底，None 是不知道），
        # 决策轮的「信号分」只看这一列，None 就不给后缀。
        rows = [
            ("600036", 0.31, "BUY", "batch", "2026-09-19"),  # 5 列老调用方
            ("000001", 0.1, "HOLD", "batch", "2026-09-19", None),  # 6 列 NULL
        ]
        score_map, _ = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["rank_pct"] is None
        assert score_map["SZ000001"]["rank_pct"] is None
        assert "rank_pct" in score_map["SH600036"]  # 键必须在，不能悄悄缺

    def test_realtime_source_is_flagged_and_counted(self):
        # source='realtime' 是盘中热集推理落库的行 —— 前端据此标「实时」，不然
        # 用户会把盘中分当昨天日频分看
        rows = [
            ("600036", 0.4, "BUY", "realtime", "2026-09-20"),
            ("000001", 0.1, "HOLD", "batch", "2026-09-20"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-20")

        assert score_map["SH600036"]["freq"] == "realtime"
        assert score_map["SZ000001"]["freq"] == "daily"
        assert realtime_rows == 1

    def test_null_score_is_kept_as_none_not_zero(self):
        rows = [("600036", None, None, "batch", None)]
        score_map, _ = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["value"] is None
        assert score_map["SH600036"]["side"] is None

    def test_as_of_falls_back_to_trade_date_when_row_lacks_it(self):
        # 老调用方（不需要 asOf 的查询）只取 4 列，索引越界不能炸
        rows = [("600036", 0.2, "BUY", "batch")]
        score_map, _ = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["asOf"] == "2026-09-19"

    def test_non_a_share_and_blank_rows_are_skipped(self):
        rows = [
            ("00700.HK", 0.9, "BUY", "batch", "2026-09-19"),
            ("", 0.5, "BUY", "batch", "2026-09-19"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map == {}
        assert realtime_rows == 0

    def test_last_row_wins_for_duplicate_symbol(self):
        # SQL 侧 DISTINCT ON 已保证每标的仅一行；万一有人改成全量取，
        # 后出现（created_at 更新）的那条必须覆盖前一条，而不是被前一条挡住
        rows = [
            ("600036", 0.1, "BUY", "batch", "2026-09-19"),
            ("600036", -0.2, "SELL", "realtime", "2026-09-19"),
        ]
        score_map, realtime_rows = build_score_map(rows, "2026-09-19")

        assert score_map["SH600036"]["value"] == -0.2
        assert score_map["SH600036"]["side"] == "SELL"
        assert realtime_rows == 1

    def test_model_identity_unifies_batch_and_realtime_for_same_model(self):
        # 同一款模型的批量行 feature_version=script_v1_<桶>，实时行
        # feature_version=因子目录、model_version=<模型 id>。哨兵要跨盘中/日频
        # 比较分数，两行的**模型身份必须相等**，否则每次批次↔实时切换都会
        # 被当成换模型而静默重锚（永远不报警）。
        rows = [
            (
                "600036",
                0.2,
                "BUY",
                "batch",
                "2026-10-20",
                0.9,
                "script_v1_mdl_x",
                "inference_script",
            ),
            (
                "000001",
                0.3,
                "BUY",
                "realtime",
                "2026-10-20",
                0.8,
                "qdb-catalog-v1",
                "mdl_x",
            ),
        ]
        score_map, _ = build_score_map(rows, "2026-10-20")

        assert score_map["SH600036"]["model_bucket"] == "script_v1_mdl_x"
        assert score_map["SZ000001"]["model_bucket"] == "script_v1_mdl_x"
        # 诊断列保留原始值：批量行桶名 / 实时行因子目录
        assert score_map["SH600036"]["bucket"] == "script_v1_mdl_x"
        assert score_map["SZ000001"]["bucket"] == "qdb-catalog-v1"
        assert score_map["SH600036"]["model_id"] == "mdl_x"
        assert score_map["SZ000001"]["model_id"] == "mdl_x"

    def test_model_identity_matches_even_when_bucket_is_truncated(self):
        # 写侧桶名截断 48 字符。实时行按同一折叠规则现算桶名，长 id 也不会
        # 与批量行身份分叉（拿完整 id 比会假切换）。
        long_id = "mdl_" + "x" * 80
        bucket = resolve_feature_version(long_id)
        rows = [
            (
                "600036",
                0.2,
                "BUY",
                "batch",
                "2026-10-20",
                None,
                bucket,
                "inference_script",
            ),
            ("000001", 0.3, "BUY", "realtime", "2026-10-20", None, "cat-x", long_id),
        ]
        score_map, _ = build_score_map(rows, "2026-10-20")

        assert score_map["SH600036"]["model_bucket"] == bucket
        assert score_map["SZ000001"]["model_bucket"] == bucket

    def test_unknown_provenance_keeps_keys_but_no_identity(self):
        # 老调用方（6 列）没有来源列：键必须在（调用方按键取值），值为 None
        # 表示「不知道」——哨兵据此拒绝跨未知来源比较。
        score_map, _ = build_score_map(
            [("600036", 0.31, "BUY", "batch", "2026-09-19", 0.882)], "2026-09-19"
        )

        assert score_map["SH600036"]["bucket"] is None
        assert score_map["SH600036"]["model_id"] is None
        assert score_map["SH600036"]["model_bucket"] is None


class TestSnapshotFreshness:
    """实时行「最后写入」应赢过之后落库的批量行。

    历史坑：``created_at`` 是首次插入时间，实时 upsert 只更新 ``signal_ts``
    （``realtime_contract`` 故意不覆盖 ``created_at``）。只看 ``created_at``
    时，盘中实时行会被**之后**跑完的批次行整天压住——用户看到的仍是日频分，
    且不同批次按完成顺序互相覆盖。
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_realtime_row_with_newer_signal_ts_wins(self, monkeypatch):
        await _require_db()
        import backend.shared.signal_scores as ss

        tenant = f"t-fresh-{uuid.uuid4().hex[:8]}"
        await _cleanup_tenant(tenant)
        try:
            monkeypatch.setattr(ss, "get_scoping_mode", lambda **kw: "off")
            await _seed_score(
                tenant,
                symbol="600036",
                score=0.9,
                source="batch",
                feature_version="script_v1_mdl_batch",
                model_version="inference_script",
                created_at=datetime(2099, 1, 6, 10, 0, tzinfo=timezone.utc),
                signal_ts=datetime(2099, 1, 6, 10, 0, tzinfo=timezone.utc),
            )
            # 实时行先插入、后更新：created_at 更早，signal_ts 更新
            await _seed_score(
                tenant,
                symbol="600036.SH",
                score=-0.5,
                source="realtime",
                feature_version="qdb-catalog-v1",
                model_version="mdl_live",
                created_at=datetime(2099, 1, 6, 9, 0, tzinfo=timezone.utc),
                signal_ts=datetime(2099, 1, 6, 11, 0, tzinfo=timezone.utc),
            )

            score_map, meta = await ss.load_score_snapshot(tenant)

            assert meta["ok"] is True
            assert score_map["SH600036"]["value"] == -0.5
            assert score_map["SH600036"]["freq"] == "realtime"
            assert score_map["SH600036"]["model_id"] == "mdl_live"
        finally:
            await _cleanup_tenant(tenant)
            await _close_db()


async def _require_db():
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _close_db():
    from backend.shared.database_manager_v2 import close_database

    await close_database()


async def _cleanup_tenant(tenant: str):
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("DELETE FROM engine_signal_scores WHERE tenant_id = :t"),
            {"t": tenant},
        )


async def _seed_score(
    tenant: str,
    *,
    symbol: str,
    score: float,
    source: str,
    feature_version: str,
    model_version: str,
    created_at: datetime,
    signal_ts: datetime,
):
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO engine_signal_scores "
                "(run_id, tenant_id, user_id, trade_date, symbol, fusion_score, "
                " model_version, feature_version, market, source, universe_tag, "
                " created_at, signal_ts) "
                "VALUES (:run, :t, 'u1', :td, :sym, :score, :mv, :fv, 'CN', :src, "
                "        'CN', :ts, :sts)"
            ),
            {
                "run": f"run_fresh_{uuid.uuid4().hex[:10]}",
                "t": tenant,
                "td": date(2099, 1, 6),
                "sym": symbol,
                "score": score,
                "mv": model_version,
                "fv": feature_version,
                "src": source,
                "ts": created_at,
                "sts": signal_ts,
            },
        )


class TestScoreFreqOf:
    """分频标注的唯一判定：只有 ``source='realtime'`` 才敢说「实时」。

    宁可把实时行标成日频（欠标 → 用户当成降级路径保守看），也不能把批次日频
    标成实时 —— 后者会让用户拿隔夜分当盘中分下单。所以未知/空/大小写不符
    一律日频。
    """

    def test_realtime_source_is_realtime(self):
        assert score_freq_of("realtime") == "realtime"

    def test_batch_source_is_daily(self):
        assert score_freq_of("batch") == "daily"

    def test_unknown_or_blank_source_is_daily(self):
        for raw in (None, "", "  ", "Realtime", "REALTIME", "realtime_v2", 0):
            assert score_freq_of(raw) == "daily"


class TestSqlConstants:
    def test_market_predicate_tolerates_null_market(self):
        # 老库 market 列未回填；裸 `market = 'CN'` 会把 CN 行整片查空（静默）。
        # 且 `'CUSTOM'`（自定义市场，a_share 适配器 + SSE 日历 + SH000300 基准构建，
        # 落库 symbol 100% A 股形态）必须与 'CN' 同等对待 —— 2026-10-13 全市场带
        # BUY/SELL 的行**只**存在于 CUSTOM 桶，漏掉它候选信号列表整片为空。
        for sql in (SQL_LATEST_COVERED_DATE, SQL_LATEST_ANY_DATE, SQL_SCORES_BY_DATE):
            assert A_SHARE_MARKET_SQL in sql
            assert "'CUSTOM'" in sql

    def test_coverage_threshold_is_bound_not_inlined(self):
        assert ":min_cov" in SQL_LATEST_COVERED_DATE
        assert MIN_SIGNAL_COVERAGE == 1000

    def test_coverage_count_folds_symbol_identity(self):
        # T4-1 审计 M2：实时行=后缀身份（600036.SH）、批量行=裸 6 位——同一只股
        # 在 COUNT(DISTINCT symbol) 下算成两只，覆盖日闸门会被热集注水假过线。
        # 计数必须先折叠市场段再 DISTINCT。
        assert "COUNT(DISTINCT CASE" in SQL_LATEST_COVERED_DATE
        assert "[.](SH|SZ|BJ)$" in SQL_LATEST_COVERED_DATE
        assert "left(symbol, 6)" in SQL_LATEST_COVERED_DATE

    def test_scores_query_picks_latest_row_per_symbol(self):
        # 去重键必须折到裸六位（SYMBOL_FOLD_KEY），不能按字面 symbol：前缀形排序恒在
        # 数字形之后，Python 侧「后写覆盖」会等价于「前缀形恒胜」，与 created_at 无关。
        assert SYMBOL_FOLD_KEY in SQL_SCORES_BY_DATE
        assert "DISTINCT ON (symbol)" not in SQL_SCORES_BY_DATE
        # 取「最后写入」而不是「首次写入」：实时 upsert 只更新 signal_ts
        # （created_at 冻结在首插），只看 created_at 会让后跑的批次整天压住实时行。
        assert "COALESCE(signal_ts, created_at) DESC" in SQL_SCORES_BY_DATE
        assert "feature_version" in SQL_SCORES_BY_DATE
        assert "model_version" in SQL_SCORES_BY_DATE

    def test_scores_query_carries_rank_pct(self):
        # rank_pct 是决策轮行情行内「信号分」的唯一原料（折算研究评分刻度）；
        # 少取这一列 = 提示词永远没有信号分，且 SQL 层无任何报错（静默空转）。
        assert "rank_pct" in SQL_SCORES_BY_DATE

    def test_scores_query_folds_prefix_form_too(self):
        # 前缀形（'SH600036'）也要折——它是自定义市场桶的唯一写法
        assert "substr(symbol, 3, 6)" in SYMBOL_FOLD_KEY
        assert "^(SH|SZ|BJ)[0-9]{6}$" in SYMBOL_FOLD_KEY
