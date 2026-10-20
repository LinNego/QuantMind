"""信号分快照取数口径（**唯一实现**）：最新覆盖充分日 + 每标的最近一条。

为什么单列一个共享模块：自选池统一视图（api 服务）与持仓哨兵（trade 服务）
必须看到**同一张分数表**——否则会出现「列表显示分数 +0.12，哨兵却按 -0.03
报警」这种自相矛盾的现场，用户没法信任任何一边。

口径（与候选信号页 `/stock-terminal/list` 同源）：

1. 取「覆盖充分日」：``COUNT(DISTINCT <标的归一键>) >= MIN_SIGNAL_COVERAGE`` 的最近
   ``trade_date``（计数把后缀身份折叠到裸 6 位——实时行 ``600036.SH`` 与批量行
   ``600036`` 是同一只，同股只算一次；T4-1 审计 M2）；当日覆盖不足（推理刚跑到
   一半/降级日）时回退到最近一天，并把回退事实交给调用方（``meta.fallback=True``）。
2. 该日每标的取**最后写入的一条**（``DISTINCT ON <裸码归一键> ORDER BY <同键>,
   COALESCE(signal_ts, created_at) DESC, id DESC``）：``created_at`` 是首次插入
   时间（实时 upsert 故意不覆盖），``signal_ts`` 才是最后写入时间——只看
   ``created_at`` 会让盘中实时行被之后跑完的批次行整天压住。同一天同一只股会有
   多行——日频批次行 / 盘中实时行（``source='realtime'``）/ 不同 run 的重复打分，
   且 **symbol 有三种写法**
   （裸 ``600036`` / 后缀 ``600036.SH`` / 前缀 ``SH600036``）。按**字面** symbol
   去重只能合并同形态的行：裸码留多行，Python 侧「后写覆盖」实际是按字面序挑
   一条（前缀形排序恒在后 → 恒胜），与 ``created_at`` 最新无关。故去重键必须先
   折到裸 6 位（:data:`SYMBOL_FOLD_KEY`）。
3. 市场口径 = :data:`A_SHARE_MARKET_SQL`——``market`` 列可空（老库未回填，裸等号
   会把这些行整片查空）；``'CUSTOM'``（自定义市场）按 A 股适配器/日历/基准构造
   （``getMarketConfig('CUSTOM')`` = a_share + SSE + SH000300），落库 symbol
   100% 是 A 股形态，必须与 ``'CN'`` 同等对待——2026-10-13 全市场带 BUY/SELL
   的行**只**存在于 ``CUSTOM`` 桶，漏掉它候选信号列表整片为空。
4. 标的键形归一为 prefix（``SH600036``）：表里是裸 6 位，持仓/自选侧是 prefix。
5. **按桶过滤（P2-0 信号桶隔离）**：表里同日多模型按
   ``feature_version='script_v1_<模型桶>'`` 并存，旧口径混读会把观察期挑战者
   的分数当生产分（2026-10-08 实测默认租户同日 2 桶 6496 行混读）。模式由
   ``signal_buckets.get_scoping_mode`` 决定：``off`` 旧口径 / ``shadow`` 旧口径
   出数 + 影子比对留痕 / ``enforce`` 只读生效模型桶（解析失败 → 空 + 原因，
   绝不退回混读）。

盘中实时分（``source='realtime'``）与日频批次写在**同一张表**，因此开启实时
推理后本快照即盘中分；未开启时这是最近一个批次日，调用方按 ``freq`` 如实标注。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from backend.shared.logging_config import get_logger
from backend.shared.signal_buckets import (
    BUCKET_PREFIX,
    get_scoping_mode,
    record_shadow_evidence,
    resolve_effective_bucket,
    resolve_feature_version,
    resolve_realtime_bucket,
    shadow_diff,
)
from backend.shared.stock_utils import StockCodeUtil

logger = get_logger(__name__)

#: 「信号日覆盖充分」判据（全市场 CN 标的数千只，覆盖不足说明推理残缺）
MIN_SIGNAL_COVERAGE = 1000

#: 覆盖计数用的标的归一表达式（T4-1 审计 M2）：实时行 symbol 是**后缀身份**
#: （``600036.SH``，000001.SH≠000001.SZ），批量行是裸 6 位——同一只股票两种形态
#: 在 ``COUNT(DISTINCT symbol)`` 下会算成两只，把覆盖闸门注水抬高（热集 ~500 只
#: 足以让一批残留日假过线）。覆盖日计数一律折叠到裸 6 位；非 A 股形态原样参与。
#: **跨模块共享**（stock_lookback 阶梯 / stock_terminal 默认信号日同判据）。
SYMBOL_COUNT_KEY = (
    "CASE WHEN symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$' THEN left(symbol, 6) ELSE symbol END"
)

#: 打分去重用的标的归一表达式：在 :data:`SYMBOL_COUNT_KEY` 之上再折「前缀形」
#: （``SH600036`` → ``600036``）。覆盖计数与打分去重必须用**同一个归一键**，否则
#: 「覆盖充分」的判据与「取哪一行」的口径不一致。前缀形自 2026-10-09 起进入本表
#: （自定义市场/CUSTOM 桶走 prefix），不折就会与裸码行并成两只、且去重时按字面序
#: 挑行。**跨模块共享**（stock_terminal 同一表达式内联在 4 条 SQL 里）。
SYMBOL_FOLD_KEY = (
    "CASE WHEN symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$' THEN left(symbol, 6) "
    "WHEN symbol ~ '^(SH|SZ|BJ)[0-9]{6}$' THEN substr(symbol, 3, 6) "
    "ELSE symbol END"
)

#: A 股市场口径谓词（**唯一出处**）：可空列 + 自定义市场别名。见模块 docstring 第 3 条。
A_SHARE_MARKET_SQL = "(market IS NULL OR market IN ('CN','CUSTOM'))"

SQL_LATEST_COVERED_DATE_TMPL = (
    "SELECT trade_date FROM engine_signal_scores "
    f"WHERE tenant_id = :tid AND {A_SHARE_MARKET_SQL} {{bucket}}"
    "GROUP BY trade_date HAVING COUNT(DISTINCT {sym_key}) >= :min_cov "
    "ORDER BY trade_date DESC LIMIT 1"
)

_CN_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)\d{6}$")

#: 三条 SQL 的模板：``{bucket}`` 占位在 ``_BUCKET_SQL`` 渲染时带上「前导 AND +
#: 尾随空格」。``{sym_key}`` 是**标的归一键**（覆盖日模板用 :data:`SYMBOL_COUNT_KEY`、
#: 打分模板用 :data:`SYMBOL_FOLD_KEY`）——该表达式含正则量词 ``{6}``，必须经
#: ``{sym_key}`` 占位传入：str.format 不重扫替换值，内联进模板则会被当替换字段报错。
#: 市场口径统一走 :data:`A_SHARE_MARKET_SQL`（f-string 内联，非 format 占位）。
SQL_LATEST_ANY_DATE_TMPL = (
    "SELECT trade_date FROM engine_signal_scores "
    f"WHERE tenant_id = :tid AND {A_SHARE_MARKET_SQL} {{bucket}}"
    "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1"
)

SQL_SCORES_BY_DATE_TMPL = (
    "SELECT DISTINCT ON ({sym_key}) symbol, fusion_score, signal_side, source, "
    "trade_date, rank_pct, feature_version, model_version "
    "FROM engine_signal_scores "
    f"WHERE tenant_id = :tid AND trade_date = :d AND {A_SHARE_MARKET_SQL} {{bucket}}"
    # 「最后写入」而非「首次插入」：实时 upsert 只更新 signal_ts（created_at
    # 故意冻结在首插），只看 created_at 会让盘中实时行被之后跑完的批次行压住。
    "ORDER BY {sym_key}, COALESCE(signal_ts, created_at) DESC, id DESC"
)

#: 桶过滤子句（enforce/shadow 按桶取数用；前导 AND + 尾随空格由这里携带）
_BUCKET_SQL = "AND feature_version = :bucket "

#: 无桶过滤渲染结果（旧口径常量照旧导出——存量引用与单测依赖其字面内容）
SQL_LATEST_COVERED_DATE = SQL_LATEST_COVERED_DATE_TMPL.format(
    bucket="", sym_key=SYMBOL_COUNT_KEY
)
SQL_LATEST_ANY_DATE = SQL_LATEST_ANY_DATE_TMPL.format(bucket="")
SQL_SCORES_BY_DATE = SQL_SCORES_BY_DATE_TMPL.format(bucket="", sym_key=SYMBOL_FOLD_KEY)


def score_freq_of(source: Any) -> str:
    """分数行来源 → 频率标注（``"realtime"`` / ``"daily"``）。

    只有 ``source == 'realtime'``（热集盘中推理落库）才敢说「实时」；``batch``、
    空值、未知来源、大小写不符一律 ``daily``。**欠标优先**：把实时行标成日频只是
    让用户保守看，把日频行标成实时会让用户拿隔夜分当盘中分下单。

    列表页（``stock_terminal``）与快照（本模块 + 自选/哨兵）共用这一处判定，
    否则同一行分数在两个页面会显示成不同频率。
    """
    return "realtime" if source == "realtime" else "daily"


def normalize_a_share_symbol(raw: Any) -> str | None:
    """任意键形 → prefix 规范形（``SH600036``）；非 A 股返回 None。

    表内 symbol 是裸 6 位（``600036``），持仓/自选侧是 prefix。``StockCodeUtil.to_prefix``
    对裸 6 位按市场规则补前缀（6→SH、0/3→SZ、4/8→BJ），带后缀的先转再补。
    """
    base = str(raw or "").strip()
    if not base:
        return None
    prefix = StockCodeUtil.to_prefix(base)
    return prefix if _CN_PREFIX_RE.match(prefix) else None


def normalize_position_symbol(raw: Any) -> str | None:
    """持仓/自选键形 → prefix 规范形；非 A 股返回 None。

    与 :func:`normalize_a_share_symbol` 的差别只有一处：持仓键可能带侧标
    （``SH600036::long``，两融），先切掉再归一。切法放这里是为了让「切侧标」
    也只有一份实现——切漏了同一只票会变成两行。
    """
    return normalize_a_share_symbol(str(raw or "").split("::", 1)[0].strip())


def _row_model_refs(
    feature_version: Any, model_version: Any
) -> tuple[str | None, str | None, str | None]:
    """行 → ``(model_id, model_bucket, raw_bucket)``（模型身份唯一口径）。

    哨兵要跨批次/实时比较分数，必须先判断「是不是同一款模型」：

    - 批量行 ``feature_version=script_v1_<模型桶>`` → 身份就是该桶；
    - 实时行 ``feature_version=因子目录``、``model_version=<模型 id>`` → 身份按
      写侧同一折叠规则算 ``script_v1_<桶>``，与同模型批量行**逐字相等**。长
      模型 id 被截断时两侧仍一致（不拿完整 id 比，避免假「换模型」）；
    - 来源不明 → 身份退化为原始 ``feature_version``；全空 → ``None``。
    """
    fv = str(feature_version or "").strip()
    mv = str(model_version or "").strip()
    if fv.startswith(BUCKET_PREFIX):
        slug = fv[len(BUCKET_PREFIX) :]
        return (slug or None), fv, fv
    if mv and mv != "inference_script":
        return mv, resolve_feature_version(mv), fv or None
    return None, (fv or None), (fv or None)


def build_score_map(
    rows: list[Any], trade_date: str
) -> tuple[dict[str, dict[str, Any]], int]:
    """DB 行 → ``{prefix: {value, side, freq, asOf, rank_pct, ...}}``。

    返回 ``(map, realtime_count)``。行序 = ``DISTINCT ON`` 的结果序（每 symbol
    仅一行，已是该标的最后写入的一条）。分数为 NULL 的行仍进 map：NaN 分数与
    「没有分数」不同，前端要能区分。

    ``rank_pct``（0–1 截面分位，``compute_rank_pct`` 口径）与
    ``bucket``/``model_id``/``model_bucket``（模型来源）均为 **additive 契约列**：
    老行/老调用方缺列时键仍在、值为 None（「不知道」不是「没有」）。行长度守卫
    与 ``asOf`` 同式：只有 4/5/6 列的老行传进来也不能炸。
    """
    score_map: dict[str, dict[str, Any]] = {}
    realtime_rows = 0
    for r in rows:
        sym = normalize_a_share_symbol(r[0])
        if not sym:
            continue
        freq = score_freq_of(r[3])
        if freq == "realtime":
            realtime_rows += 1
        model_id, model_bucket, raw_bucket = _row_model_refs(
            r[6] if len(r) > 6 else None,
            r[7] if len(r) > 7 else None,
        )
        score_map[sym] = {
            "value": float(r[1]) if r[1] is not None else None,
            "side": r[2],
            "freq": freq,
            "asOf": str(r[4])[:10] if len(r) > 4 and r[4] is not None else trade_date,
            "rank_pct": (float(r[5]) if len(r) > 5 and r[5] is not None else None),
            "bucket": raw_bucket,
            "model_id": model_id,
            "model_bucket": model_bucket,
        }
    return score_map, realtime_rows


def _bucket_filter(
    bucket: str | None,
    realtime_bucket: str | None = None,
    model_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """桶隔离过滤子句 + 绑定参数（三条查询共用，保证口径一致）。

    只按批量桶过滤会漏掉同模型的盘中实时行（实时行 ``feature_version`` 是
    因子目录版本）。带上 ``realtime_bucket`` 时扩成「批量桶 OR（实时桶 AND
    model_version = 生效模型）」——**必须限定 model_version**：多个模型可能
    共享同一因子目录，只按目录过滤就是另一种混读。
    """
    if not bucket:
        return "", {}
    model = str(model_id or "").strip()
    if realtime_bucket and model and realtime_bucket != bucket:
        return (
            "AND (feature_version = :bucket OR "
            "(feature_version = :rt_bucket AND model_version = :model_id)) ",
            {"bucket": bucket, "rt_bucket": realtime_bucket, "model_id": model},
        )
    return _BUCKET_SQL, {"bucket": bucket}


async def _fetch_snapshot(
    session: Any,
    tenant_id: str,
    bucket: str | None,
    *,
    realtime_bucket: str | None = None,
    model_id: str | None = None,
) -> tuple[Any, list[Any], bool]:
    """一次口径取数 → ``(信号日 d0, rows, 覆盖不足回退标志)``。

    ``bucket=None`` 渲染的 SQL 与旧常量逐字节相同；非空则三条查询都带桶过滤
    （覆盖充分日/回退日也按桶算——否则回退日会落到别的桶的日期上），实时行
    按 :func:`_bucket_filter` 的规则并入。
    """
    clause, bucket_params = _bucket_filter(bucket, realtime_bucket, model_id)
    params: dict[str, Any] = {
        "tid": tenant_id,
        "min_cov": MIN_SIGNAL_COVERAGE,
        **bucket_params,
    }
    d0 = (
        await session.execute(
            text(
                SQL_LATEST_COVERED_DATE_TMPL.format(bucket=clause, sym_key=SYMBOL_COUNT_KEY)
            ),
            params,
        )
    ).scalar_one_or_none()
    fallback = False
    if d0 is None:
        fallback = True
        d0 = (
            await session.execute(
                text(SQL_LATEST_ANY_DATE_TMPL.format(bucket=clause)), params
            )
        ).scalar_one_or_none()
    if d0 is None:
        return None, [], fallback
    rows = (
        await session.execute(
            text(
                SQL_SCORES_BY_DATE_TMPL.format(bucket=clause, sym_key=SYMBOL_FOLD_KEY)
            ),
            {"tid": tenant_id, "d": d0, **bucket_params},
        )
    ).fetchall()
    return d0, list(rows), fallback


async def load_score_snapshot(
    tenant_id: str,
    *,
    user_id: str | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读一次分数快照 → ``(score_map, meta)``。

    自选池统一视图与持仓哨兵**必须**经此函数取分：两处各写一份 SQL，迟早在
    「覆盖充分日回退」或「实时行取哪条」上分叉（见模块 docstring）。

    ``meta`` 带 ``signal_date``（实际取到的信号日）、``fallback``（覆盖不足回退到
    最近日）、``realtime_rows``（其中盘中实时行条数）、``ok``/``reason``，以及
    P2-0 桶隔离面 ``scoping``/``bucket``/``realtime_bucket``/``model_id``/
    ``model_source``。取数失败不抛：返回空 map + ``ok=False``，调用方按「没取到
    ≠ 分数是 0」处理。

    桶隔离（``scoping`` 模式见 ``signal_buckets``）：``model_id`` 显式传入时
    直接按该模型桶取；否则按生效模型解析（``user_id`` 缺省时用租户 is_default
    持有者）。生效桶除了批量行 ``script_v1_<桶>``，还并入**该模型自己的实时
    行**（``feature_version=因子目录`` 且 ``model_version=生效模型``）——否则
    盘中分会在 enforce 下整片消失。``enforce`` 下解析失败 → 空结果 + ``ok=False``
    + 原因（混读比没数据更危险）；``shadow`` 下照旧口径出数 + 影子比对留痕。
    """
    from backend.shared.database_manager_v2 import get_session

    mode = get_scoping_mode()
    meta: dict[str, Any] = {
        "signal_date": None,
        "fallback": False,
        "realtime_rows": 0,
        "ok": False,
        "reason": None,
        "scoping": mode,
        "bucket": None,
        "realtime_bucket": None,
        "model_id": None,
        "model_source": None,
    }
    bucket: str | None = None
    shadow_payload: dict[str, Any] | None = None
    if mode != "off":
        eff: dict[str, Any] = {}
        if str(model_id or "").strip():
            bucket = resolve_feature_version(model_id)
            meta["model_id"] = str(model_id).strip()
            meta["model_source"] = "explicit_param"
        else:
            eff = await resolve_effective_bucket(
                tenant_id=tenant_id, user_id=user_id, market="CN"
            )
            bucket = eff.get("bucket")
            meta["model_id"] = eff.get("model_id")
            meta["model_source"] = eff.get("model_source")
        meta["bucket"] = bucket
        if bucket and meta.get("model_id"):
            meta["realtime_bucket"] = await resolve_realtime_bucket(
                tenant_id, meta["model_id"]
            )
        if not bucket and mode == "enforce":
            meta["reason"] = (
                f"信号桶隔离：生效模型桶无法解析（{eff.get('reason')}）；"
                "不混读，返回空"
            )
            logger.warning("[signal_scores] tenant=%s %s", tenant_id, meta["reason"])
            return {}, meta

    try:
        async with get_session(read_only=True) as session:
            read_bucket = bucket if mode == "enforce" else None
            rt_bucket = meta.get("realtime_bucket") if read_bucket else None
            d0, rows, fallback = await _fetch_snapshot(
                session,
                tenant_id,
                read_bucket,
                realtime_bucket=rt_bucket,
                model_id=meta.get("model_id"),
            )
            meta["fallback"] = fallback
            if mode == "shadow":
                if bucket:
                    d0_b, rows_b, _ = await _fetch_snapshot(
                        session,
                        tenant_id,
                        bucket,
                        realtime_bucket=meta.get("realtime_bucket"),
                        model_id=meta.get("model_id"),
                    )
                    old_map, _ = build_score_map(
                        rows, str(d0)[:10] if d0 is not None else ""
                    )
                    new_map, _ = build_score_map(
                        rows_b, str(d0_b)[:10] if d0_b is not None else ""
                    )
                    shadow_payload = shadow_diff(
                        {s: e["value"] for s, e in old_map.items()},
                        {s: e["value"] for s, e in new_map.items()},
                        old_date=d0,
                        new_date=d0_b,
                    )
                else:
                    shadow_payload = {
                        "equal": False,
                        "bucket_missing": True,
                        "reason": eff.get("reason") or "no_effective_model",
                    }
                meta["shadow"] = shadow_payload
            if d0 is None:
                meta["ok"] = True  # 查得到，只是（该口径）库里还没有信号日
                meta["reason"] = (
                    f"engine_signal_scores 无数据（生效桶 {bucket}）"
                    if mode == "enforce"
                    else "engine_signal_scores 无数据"
                )
                if shadow_payload is not None:
                    record_shadow_evidence("watchlist", shadow_payload)
                return {}, meta
    except Exception as exc:  # noqa: BLE001 - 取不到分不是致命错（哨兵跳过本轮）
        meta["reason"] = f"分数快照取数失败: {exc}"
        logger.warning("[signal_scores] %s", meta["reason"])
        return {}, meta

    if shadow_payload is not None:
        record_shadow_evidence("watchlist", shadow_payload)
    meta["signal_date"] = str(d0)[:10]
    score_map, realtime_rows = build_score_map(list(rows), meta["signal_date"])
    meta["realtime_rows"] = realtime_rows
    meta["rows"] = len(score_map)
    meta["ok"] = True
    return score_map, meta
