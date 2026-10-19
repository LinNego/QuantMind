"""决策提示词**补块**构建（Stage 2 第一批）：盘中可获取的数据 → 提示词文本块。

细案 ``docs/arena总控回路回归_细案_20261014.md``「Stage 2 第一批」的落点：用户点名
「交易时候获取到的**模型分数、市场状态**，分数都是能获取到的」——两条数据实测都在产
（``engine_signal_scores`` 实时行 / db0 ``qm:regime:intraday``），但都不进决策提示词。
本模块把「分数快照 / regime 快照」变成两段可直接拼进提示词的文本：

* :func:`attach_signal_scores` —— 把 ``rank_pct`` 折算成研究评分刻度（0–100，
  ``research_score`` 同一公式）填进 :class:`PoolQuote.signal_score`，渲染成行情行内
  ``· 信号分 88.2`` 后缀（渲染器不动，上游金样逐字节不破）；:func:`build_signal_meta_block`
  给出处/刻度/覆盖率——分数是**该行所属推理批次内部**的截面分位（2026-10-14 实测：
  ``mode=off`` 读口径下快照混四批，口径句必须如实说「可能混合」），口径句必须跟着走。
* :func:`build_market_state_block` —— ``qm:regime:intraday``（engine 60 秒刷新）→
  【盘中市场状态】块：状态/建议仓位系数/20 日收益波动/指数现价/广度/快照年龄/notes。

两条纪律（与 ``decision/context.py`` 的「缺失一律 ``—``，绝不显示成 0」同源）：

1. **绝不编数**：``rank_pct`` 为 NULL 不给「信号分」；指数快照没读到就说「未取到」；
   regime 整包读不到 → 块渲染「本轮不可得」一行——静默消失会让「agent 看不见」
   这件事再次不可见。
2. **纯函数**：本模块不取数、不连 Redis、不读文件；快照由调用方（决策轮 IO 层）注入。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.decision.context import PoolQuote
from backend.shared.decision_context_source import day_change_pct

#: regime 快照「新鲜」窗口（分钟）：引擎 60 秒刷新，超过 5 分钟没动 = 服务停摆或
#: 非交易时段（盘前轮拿到的多是昨收后那一版）——如实标旧，不静默当实时。
MARKET_STATE_STALE_MIN = 5.0

#: regime 三态中文化（``market_regime.STATES`` 的展示面）。表外的原样回显——
#: 不认识就照抄，绝不猜。
_STATE_ZH = {"bull": "偏强", "neutral": "中性", "bear": "偏弱"}


def _f(value: object) -> float | None:
    """快照字段 → float；空/脏一律 ``None``（口径同 ``decision_context_source._f``）。

    注意 Hash 值一律是字符串，且写侧 ``str(None)`` 会留下字面 ``"None"``——
    ``float("None")`` 抛错、``int("None")`` 同样，故数值字段一律先过这里。
    """
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _int_or(value: object, default: int = 0) -> int:
    """计数字段 → int；空/脏 → ``default``（快照/元信息坏值不许炸穿提示词）。"""
    number = _f(value)
    return int(number) if number is not None else default


def attach_signal_scores(
    quotes: Sequence[PoolQuote],
    score_map: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[tuple[PoolQuote, ...], int]:
    """行情行 → 填 ``signal_score``（研究评分刻度 0–100）；返回 (新元组, 有分条数)。

    ``score_map`` 来自 :func:`backend.shared.signal_scores.load_score_snapshot`
    （prefix 键，条目带 ``rank_pct``）。折算走 :func:`research_score` ——
    与前端研究评分同公式、同金样，且与上游金样行内格式 ``· 信号分 {:.1f}``
    （0–100 刻度）**刻度兼容**：raw ``fusion_score``（量级 ±1）塞进去会渲染成
    无意义的「0.3」。

    **不编**：标的在 map 中查无、``rank_pct`` 为 NULL、``rank_pct`` 越界
    （``research_score`` 对越界返回 None——防调用方把百分数当分位传），
    三种情况一视同仁：该行不给「信号分」后缀。不可变：返回新元组。
    """
    out: list[PoolQuote] = []
    filled = 0
    if not quotes:
        return tuple(quotes), 0
    from backend.shared.research_score import research_score
    from backend.shared.signal_scores import normalize_a_share_symbol

    for q in quotes:
        score: float | None = None
        sym = normalize_a_share_symbol(q.code)
        if sym and score_map:
            entry = score_map.get(sym)
            if isinstance(entry, Mapping):
                score = research_score(entry.get("rank_pct"))
        if score is None:
            out.append(q)
            continue
        filled += 1
        out.append(replace(q, signal_score=score))
    return tuple(out), filled


def build_signal_meta_block(
    meta: Mapping[str, Any] | None, *, filled: int, total: int
) -> str:
    """【盘中模型信号】：行情行内「信号分」的出处、刻度、覆盖率。

    ``meta`` 是 ``load_score_snapshot`` 的元信息（``ok``/``reason``/``model_id``/
    ``signal_date``/``rows``/``realtime_rows``/``fallback``）。快照不可得时**必须
    说出来**并警告不要凭空引用分数——一句「没有信号分」和一个「所有票分数都是 0」
    在模型眼里相差极大。
    """
    info = meta or {}
    # ``ok=True`` 但无 ``signal_date`` = 查得到、表里没数据（推理从未落库）——
    # 对提示词而言同样是「不可得」，不能渲染成「0 只覆盖」的正常快照。
    if not info.get("ok") or not info.get("signal_date"):
        reason = str(info.get("reason") or "未接线")
        return (
            "【盘中模型信号】本轮不可得（" + reason + "）——行情行内没有「信号分」，"
            "不要凭空引用模型分数。"
        )
    model = str(info.get("model_id") or "未标注")
    date = str(info.get("signal_date") or "—")
    rows = _int_or(info.get("rows"))
    realtime_rows = _int_or(info.get("realtime_rows"))
    fallback = "，回退日（当日覆盖不足）" if info.get("fallback") else ""
    return (
        "【盘中模型信号】（行情行内「信号分」的出处）\n"
        f"- 模型 {model}（信号日 {date}{fallback} ｜ 覆盖 {rows} 只，"
        f"其中盘中实时行 {realtime_rows}）\n"
        f"- 「信号分」= 该行所属推理批次内部的截面分位（0–100，越高越靠前）；"
        "一次快照可能混合多个推理批次（各批标的集与分母不同），"
        f"跨批次/跨模型不可直接比较；本轮行情块 {int(total)} 只中 {int(filled)} 只"
        "有分，无分的不显示该字段。"
    )


def _json_obj(raw: object) -> Mapping[str, Any] | None:
    """Hash 里的 JSON 字段 → dict（写侧 ``json.dumps`` 过；替身直接给 dict 也认）。"""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, Mapping) else None
    return None


def _json_list(raw: object) -> list[Any]:
    """Hash 里的 JSON 数组字段 → list；坏 JSON/非数组 → 空表（不炸提示词）。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return []
        return list(value) if isinstance(value, (list, tuple)) else []
    return []


def _age_text(generated: datetime, now: datetime) -> str:
    """快照年龄 → 「N 分钟前 / N.N 小时前」；超新鲜窗口追加标记（不嵌套括号）。"""
    minutes = max(0.0, (now - generated).total_seconds()) / 60.0
    if minutes < 120:
        age = f"{minutes:.0f} 分钟前"
    else:
        age = f"{minutes / 60:.1f} 小时前"
    if minutes > MARKET_STATE_STALE_MIN:
        return age + "，超新鲜窗口：引擎可能停摆或非交易时段"
    return age


def build_market_state_block(
    regime: Mapping[str, Any] | None,
    index_snap: Mapping[str, Any] | None,
    now: datetime,
) -> str:
    """盘中 regime 快照 → 【盘中市场状态】块；读不到 → 如实渲染「本轮不可得」。

    ``regime`` = db0 ``qm:regime:intraday`` Hash（全字符串；写侧见
    ``services/engine/realtime_regime.py``——breadth/notes 是 JSON 编码的字段）。
    ``index_snap`` = 同一批行情快照里 ``regime["index"]`` 那一只（可缺）。

    渲染字段全部来自快照本身，**不做二次判定**（state 用快照值不重算：
    写侧 ``classify_regime`` 才是唯一分级实现）；``inputs_ok=False`` 时状态段
    附「输入不完整」标记——那时写侧的 neutral 是缺省值而不是判定值，标出来
    防模型把缺省当结论。
    """
    if not regime:
        return (
            "【盘中市场状态】本轮不可得（regime 快照缺失或读取失败：引擎实时 regime "
            "服务未开）——不要凭空断言大盘强弱。"
        )
    state_raw = str(regime.get("state") or "").strip()
    state = _STATE_ZH.get(state_raw, state_raw or "—")
    state_disp = (
        f"{state}（{state_raw}）" if state_raw and state != state_raw else state
    )
    inputs_ok = regime.get("inputs_ok")
    if inputs_ok is not None and str(inputs_ok).strip().lower() not in ("true", "1"):
        state_disp += "（输入不完整：以上为缺省判定）"

    segments = [f"状态：{state_disp}"]
    hint = _f(regime.get("position_hint"))
    if hint is not None:
        segments.append(f"建议仓位系数 {hint:.1f}")
    ret = _f(regime.get("ret"))
    window = _f(regime.get("window"))
    win_txt = f"{int(window)} 日" if window else ""
    if ret is not None:
        segments.append(f"{win_txt or '窗口'}收益 {ret * 100:+.2f}%")
    vol = _f(regime.get("vol"))
    if vol is not None:
        vol_win = f"（{win_txt}窗口）" if win_txt else ""
        segments.append(f"日波动率{vol_win}{vol * 100:.2f}%")
    lines = [
        "【盘中市场状态】（实时 regime，引擎 60 秒刷新）",
        "- " + " ｜ ".join(segments),
    ]

    index_code = str(regime.get("index") or "").strip()
    if index_code:
        price = (
            _f(
                index_snap.get("Now")
                if index_snap.get("Now") is not None
                else index_snap.get("now")
            )
            if isinstance(index_snap, Mapping)
            else None
        )
        if price is not None and price > 0:
            chg = day_change_pct(index_snap)
            chg_txt = f"（{chg:+.2f}%）" if chg is not None else ""
            lines.append(f"- 指数 {index_code}：现价 ¥{price:.2f}{chg_txt}")
        else:
            lines.append(f"- 指数 {index_code}：本轮行情未取到（不参与定价）")

    breadth = _json_obj(regime.get("breadth"))
    if breadth:
        parts = []
        for key, label in (
            ("up", "涨"),
            ("down", "跌"),
            ("flat", "平"),
        ):
            value = breadth.get(key)
            if value is not None:
                parts.append(f"{label} {value}")
        tail = []
        for key, label in (("limit_up", "涨停"), ("limit_down", "跌停")):
            value = breadth.get(key)
            if value is not None:
                tail.append(f"{label} {value}")
        sample = breadth.get("sample")
        sample_txt = f"热集样本 {sample}，" if sample is not None else ""
        line = f"- 广度（{sample_txt}不参与状态判定）：" + " ／ ".join(parts)
        if tail:
            line += "（" + " ／ ".join(tail) + "）"
        lines.append(line)

    stamp = ""
    generated = regime.get("generated_at")
    if isinstance(generated, datetime):
        moment = generated
    else:
        moment = None
        try:
            moment = datetime.fromisoformat(str(generated)) if generated else None
        except ValueError:
            moment = None
    if moment is None:
        ts = _f(regime.get("ts"))
        if ts is not None and ts > 0:
            moment = datetime.fromtimestamp(ts, tz=now.tzinfo)
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    if moment is not None:
        stamp = f"- 快照生成于 {moment:%F %T}（{_age_text(moment, now)}）"
    notes = [str(n) for n in _json_list(regime.get("notes")) if str(n).strip()]
    if stamp and notes:
        lines.append(stamp + " ｜ 注：" + "；".join(notes))
    elif stamp:
        lines.append(stamp)
    elif notes:
        lines.append("- 注：" + "；".join(notes))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Stage 2 第二批：丁1–丁9（细案「Stage 2 第二批」；全部数据门控纯函数）
#
# 同一批纪律：不取数、不连 Redis、不读文件；缺失一律显式「—/不可得」，
# 绝不编 0；未接线 → 调用方传空 → 对应块整段空串（历史字节冻结）。
# ══════════════════════════════════════════════════════════════════════

#: 展示面指数（丁5）：沪深300/上证/深成/创业板。与 hot_set ``REGIME_INDEXES``
#: 是两件事：前者=取快照的要价清单（含深市两指数，需在热集常驻源），本表=渲染名。
INDEX_DISPLAY: tuple[tuple[str, str], ...] = (
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)
INDEX_CODES: tuple[str, ...] = tuple(code for code, _ in INDEX_DISPLAY)

#: 五档失衡「强」阈值（上游 ±30% 口径；方向强弱标注用，不参与触发）。
OB_IMB_STRONG = 0.30

#: 新闻简报新鲜窗（小时）：超过仍在块里（情报仍有价值），但头部标旧。
NEWS_BLOCK_STALE_H = 8.0

#: 价格合理性上限：指数快照的五档字段实测是垃圾大数（415065178112.0），
#: 个股档位×100 以内都远小于此——用它把「指数五档」等坏值挡在渲染外。
_BOOK_PRICE_CAP = 1e7

#: 快照 ``timestamp`` 契约是秒；>1e12 一律按毫秒折算（与 decision_round_blocks 同口径）。
_MS_TS_CUTOFF = 1e12


def book_imbalance(snap: Mapping[str, Any] | None, *, levels: int = 5) -> float | None:
    """快照 → 五档失衡 ``(Σ买量−Σ卖量)/(Σ买量+Σ卖量)``；不可验证一律 ``None``。

    单位=手（TDX 帧口径）。守卫（对齐 ``redis_series_quote`` 的「不可验证一律
    None，绝不返回 0 冒充」）：任一档价格非有限/越界（指数垃圾值/0）→ ``None``；
    档位缺失按缺档求和（至少要有买一卖一）；两侧量全 0 → ``None``。
    """
    if not isinstance(snap, Mapping):
        return None
    bid_v = 0.0
    ask_v = 0.0
    seen = 0
    for i in range(1, levels + 1):
        bp = _f(snap.get(f"bid{i}"))
        ap = _f(snap.get(f"ask{i}"))
        for price in (bp, ap):
            if price is not None and not 0 < price < _BOOK_PRICE_CAP:
                return None  # 出现的价格必须合理（指数帧垃圾值 → 整票拒）
        if i == 1 and (bp is None or ap is None):
            return None  # 买一卖一缺任一 = 盘口不可验证
        if bp is not None:
            bv = _f(snap.get(f"bid_vol{i}"))
            if bv is not None and bv >= 0:
                bid_v += bv
                seen += 1
        if ap is not None:
            av = _f(snap.get(f"ask_vol{i}"))
            if av is not None and av >= 0:
                ask_v += av
                seen += 1
    if seen < 2 or bid_v + ask_v <= 0:
        return None
    return (bid_v - ask_v) / (bid_v + ask_v)


def _pct2(value: float | None) -> str:
    """百分比单元格：缺失 → ``—``（口径同 ``_pct``，简写版）。"""
    if value is None or not isinstance(value, (int, float)) or value != value:
        return "—"
    return f"{value:+.2f}%"


def _lots_text(shares: float | None) -> str:
    """股 → 手文本（series volume 契约：手；差值为股，显示折回手）。"""
    if shares is None or not isinstance(shares, (int, float)) or shares != shares:
        return "—"
    return f"{shares / 100.0:,.0f} 手"


def _snap_now(snap: Mapping[str, Any] | None) -> float | None:
    if not isinstance(snap, Mapping):
        return None
    return _f(snap.get("Now") if snap.get("Now") is not None else snap.get("now"))


def _book_ladder(snap: Mapping[str, Any], side: str, levels: int = 5) -> str:
    """买/卖 1→5 档 ``价×量`` 串（量=手）；坏价/0 价逐档跳过，整侧无有效档 → ``—``。"""
    parts: list[str] = []
    for i in range(1, levels + 1):
        px = _f(snap.get(f"{side}{i}"))
        if px is None or not 0 < px < _BOOK_PRICE_CAP:
            continue
        vol = _f(snap.get(f"{side}_vol{i}"))
        if vol is not None and vol >= 0:
            parts.append(f"{px:.2f}×{vol:,.0f}")
        else:
            parts.append(f"{px:.2f}")
    return " ".join(parts) or "—"


def _amount_text(snap: Mapping[str, Any]) -> str:
    """快照成交额（TDX 契约单位=万元）→ 亿元文本；缺失/坏值 → ``—``。"""
    amt = _f(snap.get("Amount"))
    if amt is None or amt <= 0:
        return "—"
    return f"{amt / 1e4:,.2f} 亿"


def _snap_age_s(snap: Mapping[str, Any] | None, now: datetime) -> float | None:
    """快照 ``timestamp`` → 距 ``now`` 的秒数（毫秒戳自动折算）；坏值 → ``None``。"""
    if not isinstance(snap, Mapping):
        return None
    try:
        ts = float(snap.get("timestamp"))
    except (TypeError, ValueError):
        return None
    if ts > _MS_TS_CUTOFF:
        ts /= 1000.0
    if ts <= 0:
        return None
    return max(0.0, now.timestamp() - ts)


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    """``market:series`` 每码摘要（feeds 层产出、本层消费）。

    ``momentum*_pct`` = 现价相对 (last_ts − N 分钟) 前最近成员的涨跌 %；
    ``vol5m_shares`` = 窗口内**同源**累计量差（股；口径与 ``redis_series_quote.
    recent_traded_shares`` 同源，不可验证 → ``None``）。
    """

    last_ts: float = 0.0
    last_price: float | None = None
    mom30m_pct: float | None = None
    mom5m_pct: float | None = None
    vol5m_shares: float | None = None
    source: str = ""


def build_orderbook_block(
    holdings: Sequence[Any],
    snaps: Mapping[str, Mapping[str, Any]] | None,
    series: Mapping[str, SeriesSummary] | None,
    now: datetime,
    pool: Sequence[Any] = (),
) -> str:
    """丁1【实时盘口】：持仓 + 候选池逐股五档买卖盘/失衡/跳空/短涨速。

    ``pool``（2026-10-19 用户口径「L2 盘口都喂」）：候选池票与持仓同口径渲染
    （买/卖各 1–5 档 价×量），行首 ``[持仓]``/``[池]`` 区分；同一代码两处都有
    时只出一行（持仓优先）。指数帧的五档实测是垃圾大数，``book_imbalance``
    与 ``_book_ladder`` 都逐档拒掉，坏帧绝不会被当成盘口。

    快照/字段缺失一律显式 ``—``；整段无快照 → 「本轮不可得」；无对象 → 空串
    （空串即整段不出现在提示词，字节冻结纪律）。
    """
    if not holdings and not pool:
        return ""
    snaps = snaps or {}
    series = series or {}
    held = {str(h.code) for h in holdings}
    entries: list[tuple[str, Any]] = [("持仓", h) for h in holdings]
    entries.extend(("池", q) for q in pool if str(q.code) not in held)
    lines: list[str] = []
    missing = 0
    for role, item in entries:
        code = str(item.code)
        snap = snaps.get(code)
        if not snap:
            missing += 1
            continue
        now_px = _snap_now(snap)
        chg = day_change_pct(snap)
        head = f"- [{role}] {item.name}({code})"
        if now_px is not None and now_px > 0:
            head += f" 现价 {now_px:.2f}（{_pct2(chg)}）"
        head += f" ｜ 买 {_book_ladder(snap, 'bid')} ｜ 卖 {_book_ladder(snap, 'ask')}"
        imb = book_imbalance(snap)
        if imb is None:
            head += " ｜ 五档失衡 —（盘口不可验证）"
        else:
            strength = ""
            if abs(imb) >= OB_IMB_STRONG:
                strength = "，买盘强" if imb > 0 else "，卖盘强"
            head += f" ｜ 五档失衡 {imb:+.1%}（强阈 ±{OB_IMB_STRONG:.0%}{strength}）"
        # 跳空：开盘 vs 昨收；两个价都在才算。
        open_px = _f(snap.get("Open"))
        pre_close = _f(snap.get("PreClose"))
        if open_px and pre_close and pre_close > 0 and open_px > 0:
            gap = (open_px - pre_close) / pre_close * 100.0
            head += f" ｜ 跳空 {gap:+.2f}%（开 {open_px:.2f}）"
        head += f" ｜ 成交额 {_amount_text(snap)}"
        summary = series.get(code)
        head += f" ｜ 5分钟涨速 {_pct2(summary.mom5m_pct if summary else None)}"
        age = _snap_age_s(snap, now)
        if age is not None:
            head += f" ｜ 快照 {age:.0f}s"
        lines.append(head)
    if not lines:
        if missing:
            return (
                "【实时盘口】本轮不可得（持仓/池快照缺失——行情源或热集未覆盖这些标的）。"
                "五档/跳空/短涨速均无数据，不要凭记忆里的价格判断盘口。"
            )
        return ""
    header = (
        "【实时盘口】（持仓+候选池逐股五档；买/卖各列 1–5 档，格式 价×量、量单位手。"
        "量比：5 日均量源未接入，不渲染。指数五档在行情帧里是坏值，天然不出现）"
    )
    if missing:
        lines.append(f"- （另有 {missing} 只标的（持仓/池）本轮快照未取到）")
    return "\n".join([header] + lines)


def build_minute_block(
    holdings: Sequence[Any],
    series: Mapping[str, SeriesSummary] | None,
    now: datetime,
) -> str:
    """丁3【分钟特征】：mom30m/mom5m/vol5m（同源口径）；量比无源不渲染。"""
    if not holdings:
        return ""
    series = series or {}
    if not series:
        return (
            "【分钟特征】本轮不可得（行情序列 ``market:series`` 无成员——订阅引擎"
            "未运行、断流或非交易时段）。30 分钟/5 分钟动量与短窗成交量均缺位。"
        )
    lines = []
    for h in holdings:
        s = series.get(str(h.code))
        lines.append(
            f"- {h.name}({h.code})：30 分钟 {_pct2(s.mom30m_pct if s else None)}"
            f" ｜ 5 分钟 {_pct2(s.mom5m_pct if s else None)}"
            f" ｜ 5 分钟量 {_lots_text(s.vol5m_shares if s else None)}（同源差）"
        )
    header = "【分钟特征】（market:series 实时序列；量与撮合同一口径=手、同源）"
    return "\n".join([header] + lines)


def build_l2_block() -> str:
    """丁2【L2 微观结构】门控块：QM 无盘中逐笔 L2 源——如实不可得，永不静默。"""
    return (
        "【L2 微观结构】本轮不可得——QM 未接入盘中逐笔 L2 源（内外盘/大单/主力"
        "资金等逐笔指标缺位；日频 L2 因子在 QuantDB，不构成盘中口径）。盘口类"
        "异动的替代口径是五档失衡（见【实时盘口】，那是代理、不是 L2）。"
        "不要凭空推断逐笔资金流向。"
    )


def parse_brief_ts(brief: Mapping[str, Any] | None) -> float | None:
    """新闻简报 ``ts``（ISO，含 +08:00）→ epoch 秒；坏值 → ``None``。"""
    raw = (brief or {}).get("ts")
    if isinstance(raw, (int, float)) and raw == raw and raw > 0:
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # 契约是 aware（+08:00）；naive 当坏值，不猜时区
    return moment.timestamp()


def build_news_block(
    brief: Mapping[str, Any] | None,
    watch: Mapping[str, Any] | None,
    held_codes: Sequence[str],
    now: datetime,
    *,
    wired: bool = True,
) -> str:
    """丁4【盘中新闻】：简报持仓命中 + 池外异动观察（``intraday_watch``）。

    ``wired=False``（未接线）→ 空串；接线但文件缺失 → 「本轮不可得」；文件在但
    本轮没有持仓相关条目 → 只出头部（「无持仓相关」一行）——**读不到与没新闻
    是两件事**，前者必须说出来。简报 ts 超 :data:`NEWS_BLOCK_STALE_H` 小时 → 标旧。
    """
    if not wired:
        return ""
    held = {str(c) for c in held_codes}
    if not brief:
        return (
            "【盘中新闻】本轮不可得（news_brief 简报文件缺失或未产出——"
            "持仓相关消息面本轮缺位，不要凭空引用未在上下文里的新闻）。"
        )
    lines: list[str] = []
    headers_hits = 0
    for item in _json_list(brief.get("holdings")):
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        if code not in held:
            continue
        verdict = str(item.get("verdict") or "情报")
        name = str(item.get("name") or "")
        event = str(item.get("event_type") or "")
        headline = str(item.get("headline") or "")
        source = str(item.get("source") or "")
        note = str(item.get("note") or "")
        tail = f"（{source}）" if source else ""
        extra = f"；{note}" if note else ""
        lines.append(f"- [{verdict}] {name}({code}) {event}：{headline}{tail}{extra}")
        headers_hits += 1
    watch_lines: list[str] = []
    for item in _json_list((watch or {}).get("items")):
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in held:
            continue
        why = str(item.get("why") or "").strip()
        watch_lines.append(f"- 池外异动 {code}：{why}")
    watch_lines = watch_lines[:5]

    stamp = parse_brief_ts(brief)
    head = "【盘中新闻】（news_brief 简报：持仓命中 + 池外异动）"
    if stamp is not None:
        age_h = max(0.0, (now.timestamp() - stamp)) / 3600.0
        if age_h > NEWS_BLOCK_STALE_H:
            head += f"——简报已 {age_h:.1f} 小时未更新，当旧闻读"
    if not headers_hits:
        lines.append("- （本轮简报无持仓相关条目）")
    if watch_lines:
        lines.append("- 池外异动观察（非持仓，信息面参考）：")
        lines.extend(watch_lines)
    return "\n".join([head] + lines)


def build_indices_block(
    snaps: Mapping[str, Mapping[str, Any]] | None,
    now: datetime,
) -> str:
    """丁5【大盘指数】：四只指数现价+涨跌+成交额；板块行门控（无源，如实标注）。"""
    snaps = snaps or {}
    lines = [
        "【大盘指数】（实时快照；成交额=指数当日累计成交额，"
        "板块涨幅源未接入——行业面见「行业风险」段）"
    ]
    for code, name in INDEX_DISPLAY:
        snap = snaps.get(code)
        price = _snap_now(snap)
        if price is not None and price > 0:
            chg = day_change_pct(snap)
            lines.append(
                f"- {name}：{price:,.2f}（{_pct2(chg)}）｜成交额 {_amount_text(snap)}"
            )
        else:
            lines.append(f"- {name}：本轮未取到")
    return "\n".join(lines)


#: 丁9 一致性规则（移植上游 ``live_hourly_analysis.py:688-702`` 的行为约束）。
CONSISTENCY_RULES = (
    "一致性规则：反转必须有硬理由（新事实/新价格），否则沿用上一轮判断；"
    "上一轮已卖出、现已不在持仓里的股票不要再输出 sell；同一只票不要在一日内"
    "无理由来回翻转。"
)


def _decision_line(rec: Any) -> str:
    action_zh = {"buy": "买入", "sell": "卖出"}.get(str(rec.action), str(rec.action))
    pct = ""
    if rec.pct is not None:
        try:
            pct = f" {float(rec.pct):.0%}"
        except (TypeError, ValueError):
            pct = ""
    reason = str(rec.reason or "").strip()
    if len(reason) > 60:
        reason = reason[:60] + "…"
    tail = f"：{reason}" if reason else ""
    return f"- {action_zh}{pct} {rec.code or rec.code_raw}{tail}"


def _actionable(rows: Sequence[Any]) -> list[Any]:
    """可参考的决策行：买/卖（hold 不进互看块——那是噪音）。"""
    return [r for r in rows if str(getattr(r, "action", "")) in ("buy", "sell")]


#: 台账时刻的统一展示时区（北京时间）。
_CST = ZoneInfo("Asia/Shanghai")


def _cst_hm(dt: datetime) -> str:
    """台账时刻 → 北京时间 ``HH:MM``。

    ``decided_at`` 列是 TIMESTAMPTZ（aware UTC）——直接 ``%H:%M`` 渲染出的是
    UTC 墙钟（09:35 CST 显示成 01:35，2026-10-15 盘中审计）。naive 值按写入
    契约解释为 UTC 再换算；已经是北京时间的输入换算后不变。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CST).strftime("%H:%M")


def build_prev_round_block(rows: Sequence[Any], agent: str, now: datetime) -> str:
    """丁9【上一轮你的建议】：本 agent 今日最近一轮（买入/卖出行）+ 一致性规则。"""
    mine = [r for r in rows if str(getattr(r, "agent", "")) == agent]
    if not mine:
        return "【上一轮你的建议】本轮为今日首轮分析，无上一轮建议。"
    latest_round = max(mine, key=lambda r: (r.decided_at, r.id)).round_id
    round_rows = _actionable([r for r in mine if r.round_id == latest_round])
    if not round_rows:
        return (
            "【上一轮你的建议】（"
            f"{_cst_hm(max(r.decided_at for r in mine))} 一轮全部为 hold）"
            "——" + CONSISTENCY_RULES
        )
    lines = [
        f"【上一轮你的建议】（{_cst_hm(round_rows[0].decided_at)}，{len(round_rows)} 条）"
    ]
    lines.extend(_decision_line(r) for r in round_rows)
    lines.append(CONSISTENCY_RULES)
    return "\n".join(lines)


def build_cross_agent_block(rows: Sequence[Any], agent: str) -> str:
    """丁6【其他 agent 动态】：每家最新一轮的买/卖行（互看防同码对撞）。"""
    others = [r for r in rows if str(getattr(r, "agent", "")) != agent]
    if not others:
        return "【其他 agent 动态】今日暂无可参考的他家决策。"
    by_agent: dict[str, list[Any]] = {}
    for r in others:
        by_agent.setdefault(str(r.agent), []).append(r)
    lines = ["【其他 agent 动态】（各家今日最新一轮的买卖动作；同码反向注意对撞）"]
    for name, group in sorted(by_agent.items()):
        latest_round = max(group, key=lambda r: (r.decided_at, r.id)).round_id
        picks = _actionable([r for r in group if r.round_id == latest_round])
        stamp = max(r.decided_at for r in group if r.round_id == latest_round)
        if not picks:
            lines.append(f"- {name}（{_cst_hm(stamp)} 一轮）：全 hold")
            continue
        for r in picks:
            lines.append(f"- {name}（{_cst_hm(stamp)}）：{_decision_line(r)[2:]}")
    return "\n".join(lines)


_TIER_KEY_LABELS: tuple[tuple[str, str, str], ...] = (
    ("leverage_max", "杠杆上限", "{:.2f}"),
    ("per_stock_pct", "单票买入上限", "{:.0%}"),
    ("max_new_buys", "单轮最多新开", "{:.0f} 只"),
    ("leverage_trim_to", "减仓线（触达降至）", "{:.2f}"),
    ("per_stock_pos_pct", "单票持仓上限", "{:.0%}"),
)


def build_tier_block(tier: Any) -> str:
    """丁7【风险预算档位】：当前档位五键 + 「本轮已按其收紧」。

    ``tier`` = ``risk/tiers.TierState``；``None``（未接线）→ 空串。档位未配置
    （budget 空）照样出块：说「按默认防守参数」比让模型假设无约束强。
    """
    if tier is None:
        return ""
    budget = getattr(tier, "budget", None) or {}
    level = str(getattr(tier, "level", "") or "")
    label = str(getattr(tier, "label", "") or "")
    source = str(getattr(tier, "source", "") or "")
    head = f"【风险预算档位】（{level or '未配置'}{f'·{label}' if label else ''}，来源 {source or '—'}）"
    lines = [head]
    parts: list[str] = []
    for key, zh, fmt in _TIER_KEY_LABELS:
        value = budget.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{zh} {fmt.format(value)}")
    if parts:
        lines.append("- " + " ｜ ".join(parts))
        lines.append("- 本轮的询价/开仓/减仓按此档位收紧（成交与风控段仍为准）。")
    else:
        lines.append("- 档位未配置（无预算文档）：按默认防守参数执行。")
    if getattr(tier, "problems", ()):
        lines.append("- 档位异常：" + "；".join(str(p) for p in tier.problems[:3]))
    return "\n".join(lines)


def build_freshness_block(alerts: Sequence[str]) -> str:
    """丁8【数据新鲜度警告】：各块降级留痕汇总（置顶渲染）。无降级 → 空串。"""
    items = [str(a) for a in alerts if str(a).strip()]
    if not items:
        return ""
    lines = ["【数据新鲜度警告】以下数据源本轮降级/缺位，判断前先看这里："]
    lines.extend(f"- {a}" for a in items)
    return "\n".join(lines)


# ── 镜像闸门 / 执行受阻回注（2026-10-15 盘中审计修的 P0-B/P0-C）─────────────
# 背景：提示词自己的额度框架是「虚拟账本 × 比例」（AGENT_QUOTA_TOTAL=¥10 万，
# 单票 10–20% ⇒ 单票预算 ¥1–2 万），而真单镜像对单笔/日累计另有硬上限
# （单笔 ¥10,000 等）——模型按前者规划、被后者逐笔静默跳过（601985 买 ¥10,362
# 超单笔上限、600338 卖漂移 2.15% 超 2%），全天零反馈 → 下一轮继续产出必被
# 跳的腿。两个块对治：约束先讲清（B），跳过回注（C）。

#: 回注明细的渲染上限（再多只列最近的 N 条，其余折进尾注一行）。
_MIRROR_SKIP_RENDER_LIMIT = 10

#: 跳过原因 → 一句话中文。**表外原因原样回显**——编词会把「不知道」化妆成
#: 「知道」（reason 词汇表的唯一出处是 ``real_mirror_service`` 的 record_skip 各调用点）。
_MIRROR_SKIP_ZH: dict[str, str] = {
    "max_order_value": "单笔金额超上限",
    "max_daily_value": "当日金额额度已用满",
    "max_daily_orders": "当日笔数额度已用满",
    "max_daily_symbols": "当日标的名额已用满",
    "price_drift": "委托价与参考价漂移超限",
    "price_sanity": "价格异常（偏离过大按脏数据拒发）",
    "no_reference_price": "参考价不可得",
    "outside_trading_hours": "非交易时段",
    "whitelist": "不在镜像白名单",
    "blacklist": "命中黑名单",
    "insufficient_cash": "账户可用资金不足",
    "insufficient_position": "账户可用持仓不足",
    "queue_duplicate": "同一委托已入队（去重）",
    "mirror_disabled": "镜像未启用",
    "quota_error": "限额校验异常",
    "redis_unavailable": "Redis 不可用",
}


def _skip_reason_zh(reason: str) -> str:
    """跳过原因 → 展示文案；带 ``:`` 后缀的族（account_unavailable:…）单独收。"""
    text = str(reason or "")
    if text in _MIRROR_SKIP_ZH:
        return _MIRROR_SKIP_ZH[text]
    head, _, tail = text.partition(":")
    if head == "account_unavailable":
        return f"账户不可用（{tail or '未报告原因'}）"
    if head == "market_not_supported":
        return f"市场不支持镜像（{tail or '—'}）"
    if text.startswith("invalid_"):
        return "委托参数非法（被闸门拒收）"
    return text


def _money(value: object) -> str:
    """金额 → ``¥10,362``；非正/坏值 → 空串（不编数）。"""
    amount = _f(value)
    return f"¥{amount:,.0f}" if amount is not None and amount > 0 else ""


def build_mirror_gates_block(limits: Mapping[str, Any] | None) -> str:
    """P0-B【真单镜像闸门】：把**生效**硬约束送进提示词（仅镜像启用时调用）。

    ``limits`` = ``real_mirror_service.prompt_mirror_context`` 的生效配置五项
    （env 基线经 ``mirror:config`` 覆盖后的实际值）——提示词要讲的是生效值，不是
    默认值。单项缺失不编：该分句整体省略（渲染出的每个数字都必须是真闸门）。
    """
    order_cap = _f((limits or {}).get("max_order_value"))
    daily_value = _f((limits or {}).get("max_daily_value"))
    daily_orders = _f((limits or {}).get("max_daily_orders"))
    daily_symbols = _f((limits or {}).get("max_daily_symbols"))
    slip = _f((limits or {}).get("max_slippage_pct"))
    per_order: list[str] = []
    if order_cap is not None and order_cap > 0:
        per_order.append(f"单笔金额上限 {_money(order_cap)}")
    if slip is not None and slip > 0:
        per_order.append(f"委托价与参考价偏离 > {slip:.0%} 的腿拒发")
    daily: list[str] = []
    if daily_value is not None and daily_value > 0:
        daily.append(f"金额 ≤ {_money(daily_value)}")
    if daily_orders is not None and daily_orders > 0:
        daily.append(f"笔数 ≤ {daily_orders:.0f}")
    if daily_symbols is not None and daily_symbols > 0:
        daily.append(f"标的 ≤ {daily_symbols:.0f} 只")
    lines = ["【真单镜像闸门】（你的腿会按下列硬约束逐笔过滤；被过滤的腿不下真单）"]
    if per_order:
        lines.append("- " + "；".join(per_order))
    if daily:
        lines.append(
            "- 当日累计：" + " ｜ ".join(daily) + "（任一触顶后，当日新腿全部不过）"
        )
    lines.append(
        "- 规划金额按上面的单笔上限换算：虚拟账本额度 ≠ 真单额度，超限的大单只会被跳过。"
    )
    return "\n".join(lines)


def _mirror_skip_line(entry: Mapping[str, Any]) -> str:
    """一条跳过明细 → ``- 09:35 601985.SH 买入 1100 股（约 ¥10,362） · 单笔金额超上限``。

    ``at`` 是写侧本地墙钟 ISO（``real_mirror_service._entry_detail``），直接切
    ``HH:MM``；缺失不猜时间（该格整体省略）。
    """
    at = str(entry.get("at") or "")
    stamp = at[11:16] if len(at) >= 16 and at[10] == "T" else ""
    side = {"BUY": "买入", "SELL": "卖出"}.get(str(entry.get("side") or "").upper(), "")
    qty = _f(entry.get("quantity"))
    qty_text = f"{qty:g} 股" if qty is not None and qty > 0 else ""
    count = int(_f(entry.get("count")) or 1)
    repeat = f"（当日 {count} 次）" if count > 1 else ""
    amount = _money(entry.get("value"))
    amount_text = f"（约 {amount}）" if amount else ""
    drift = _f(entry.get("drift"))
    drift_text = f"（偏离 {drift:g}%）" if drift is not None and drift > 0 else ""
    head = " ".join(x for x in (stamp, str(entry.get("symbol")), side, qty_text) if x)
    tail = _skip_reason_zh(str(entry.get("reason") or ""))
    return f"- {head}{repeat}{amount_text}{drift_text} · {tail}"


def build_mirror_feedback_block(
    skips: Sequence[Mapping[str, Any]] | None,
) -> str:
    """P0-C【今日真单执行受阻】：当日镜像跳过明细回注（同意图重发仍会被跳）。

    ``skips`` = ``prompt_mirror_context`` 的当日明细（``at`` 降序）：
    ``None`` = 台账读不到（如实说，**读不到 ≠ 没有**）；``[]`` = 今日无跳过 →
    空串（整段不出现）。行内金额/漂移只在写侧给了才是真值（旧记录没有 → 省略），
    原因经 :func:`_skip_reason_zh` 翻译、表外原样。
    """
    if skips is None:
        return (
            "【今日真单执行受阻】跳过台账读取失败——本轮无法回注"
            "（读不到与没有是两件事，按「可能有已跳过的腿」处理）。"
        )
    rows = [e for e in skips if isinstance(e, Mapping) and e.get("symbol")]
    if not rows:
        return ""
    total = sum(int(_f(e.get("count")) or 1) for e in rows)
    lines = [
        f"【今日真单执行受阻】（模拟腿镜像真单时被闸门跳过 {total} 次；"
        "原样重发同样会被跳过——要执行请调整金额/价格）"
    ]
    for entry in rows[:_MIRROR_SKIP_RENDER_LIMIT]:
        lines.append(_mirror_skip_line(entry))
    if len(rows) > _MIRROR_SKIP_RENDER_LIMIT:
        lines.append(
            f"- ……另有 {len(rows) - _MIRROR_SKIP_RENDER_LIMIT} 条，见双轨对账报表"
        )
    return "\n".join(lines)
