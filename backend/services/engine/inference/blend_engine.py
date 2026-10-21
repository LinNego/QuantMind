"""模型组合（blend）打分：成员分位 → 组合分 → 位置词。

这是「组合选股」后端唯一一份打分实现（研究脚本 ``combo_eval`` 是它的离线对拍版）：

- 成员分位与 ``signal_contract.compute_rank_pct`` **同口径**：并列取最小名次、
  ``n == 1 → 0.0``、非有限值不参与但**计入 n**；
- 组合分 ``S = Σ(w_i × pct_i) / Σ(w_i)``，只对当日有分的成员求和——
  缺失成员重新归一，**绝不按 0 分**（0 分是「极度看空」，缺分是「没意见」）；
- 覆盖门槛与共识门槛先于排序生效，未过门槛的标的不进入榜单；
- 位置词只输出 ``BUY/HOLD/SELL`` 三个内部枚举，展示层统一译成
  靠前/居中/靠后（``backend/shared/research_score.signal_position_label``）。

纯函数、无 IO：DB/缓存/名称补全都在 service 层，便于单测锁口径。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any
from collections.abc import Collection, Mapping, Sequence

from backend.shared.research_score import research_score as compute_research_score
from backend.shared.signal_contract import compute_rank_pct

#: ``S_pct`` 位置切线（与设计稿 §4.3 一致）。
POSITION_TOP_THRESHOLD = 0.70
POSITION_BOTTOM_THRESHOLD = 0.30

#: 共识门槛的「共识」定义：成员分位 ≥ 该值算一票。
CONSENSUS_MEMBER_THRESHOLD = 0.70

#: 分歧度分档（成员分位样本标准差）。
DISPERSION_MEDIUM = 0.12
DISPERSION_HIGH = 0.22

POSITION_BUY = "BUY"
POSITION_HOLD = "HOLD"
POSITION_SELL = "SELL"


@dataclass(frozen=True)
class BlendRow:
    """单只标的的组合结果（不可变）。"""

    symbol: str
    score: float
    rank: int
    coverage: int
    dispersion: float | None
    pct: float | None
    research_score: float | None
    position: str
    contributions: Mapping[str, float]
    missing_members: tuple[str, ...]


@dataclass(frozen=True)
class BlendResult:
    """一次组合打分的完整结果。"""

    rows: tuple[BlendRow, ...]
    member_counts: Mapping[str, int]
    stats: Mapping[str, Any]


def percent_rank_map(scores: Mapping[str, float]) -> dict[str, float]:
    """单成员当日截面分位：``symbol → pct``（缺失/非有限值不返回）。

    排序只影响确定性，不影响分位值；按 symbol 排序保证同输入同输出。
    """
    symbols = sorted(scores)
    if not symbols:
        return {}
    values = [scores[symbol] for symbol in symbols]
    pcts = compute_rank_pct(values)
    return {
        symbol: float(pct)
        for symbol, pct in zip(symbols, pcts, strict=True)
        if pct is not None
    }


def member_percent_ranks(
    member_scores: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, float]]:
    """每个成员各自算当日全截面分位（分母是该成员当日的全部标的）。"""
    return {
        model_id: percent_rank_map(scores) for model_id, scores in member_scores.items()
    }


def position_of(pct: float | None) -> str:
    """``S_pct`` → 内部位置枚举。缺失按居中处理（不替缺失编方向）。"""
    if pct is None or not math.isfinite(float(pct)):
        return POSITION_HOLD
    value = float(pct)
    if value >= POSITION_TOP_THRESHOLD:
        return POSITION_BUY
    if value <= POSITION_BOTTOM_THRESHOLD:
        return POSITION_SELL
    return POSITION_HOLD


def dispersion_band(dispersion: float | None) -> str:
    """分歧度 → ``low/medium/high``；缺失为 ``unknown``。"""
    if dispersion is None or not math.isfinite(float(dispersion)):
        return "unknown"
    value = float(dispersion)
    if value >= DISPERSION_HIGH:
        return "high"
    if value >= DISPERSION_MEDIUM:
        return "medium"
    return "low"


def _member_weight(weights: Mapping[str, float], model_id: str) -> float:
    weight = float(weights.get(model_id, 1.0))
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError(f"成员权重必须是正的有限数：{model_id}={weight}")
    return weight


def _sample_std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    return float(statistics.stdev(values))


def combine_member_ranks(
    ranks: Mapping[str, Mapping[str, float]],
    members: Sequence[str],
    *,
    method: str = "mean",
    weights: Mapping[str, float] | None = None,
    min_coverage: int = 2,
    trim: int = 2,
    consensus_min_count: int = 0,
    consensus_threshold: float = CONSENSUS_MEMBER_THRESHOLD,
) -> BlendResult:
    """把成员分位合成组合分并排序。

    ``ranks`` 是 ``member_percent_ranks`` 的输出；``members`` 的顺序只影响
    缺失成员列表的展示顺序，不影响分数。

    ``method``：
    - ``mean``：加权平均（缺失成员在可用成员间重新归一）；
    - ``median``：可用成员中位数（忽略权重）；
    - ``trimmed``：去掉最高/最低各 ``trim`` 个后的均值（可用 < ``2*trim+1`` → 不参与）。
    """
    ordered_members = list(dict.fromkeys(str(m) for m in members))
    if not ordered_members:
        return BlendResult(rows=(), member_counts={}, stats={"n_members": 0})
    if method not in {"mean", "median", "trimmed"}:
        raise ValueError(f"未知组合方法：{method}")
    if method == "trimmed" and trim < 0:
        raise ValueError("trim 不能为负")
    weight_map = {
        member: _member_weight(weights or {}, member) for member in ordered_members
    }
    if min_coverage < 1:
        raise ValueError("min_coverage 必须 ≥1")
    if consensus_min_count < 0:
        raise ValueError("consensus_min_count 不能为负")
    if not 0.0 <= float(consensus_threshold) <= 1.0:
        raise ValueError("consensus_threshold 必须在 [0,1]")

    member_counts = {m: len(ranks.get(m, {})) for m in ordered_members}
    symbols = sorted({symbol for m in ordered_members for symbol in ranks.get(m, {})})

    collected: list[
        tuple[str, float, int, float | None, dict[str, float], tuple[str, ...]]
    ] = []
    for symbol in symbols:
        contributions: dict[str, float] = {}
        pcts: list[float] = []
        missing: list[str] = []
        for member in ordered_members:
            pct = ranks.get(member, {}).get(symbol)
            if pct is None:
                missing.append(member)
                continue
            pcts.append(float(pct))
            contributions[member] = weight_map[member] * float(pct)
        coverage = len(pcts)
        if coverage < min_coverage:
            continue
        if consensus_min_count > 0:
            ballots = sum(1 for pct in pcts if pct >= consensus_threshold)
            if ballots < consensus_min_count:
                continue
        if method == "trimmed":
            if coverage < 2 * trim + 1:
                continue
            ordered = sorted(pcts)
            kept = ordered[trim : coverage - trim]
            score = sum(kept) / len(kept)
            # 中位数/截尾均值没有可加的权重分解，贡献留空（前端展示成员分位本身）。
            normalized = {}
        elif method == "median":
            score = float(statistics.median(pcts))
            # 中位数/截尾均值没有可加的权重分解，贡献留空（前端展示成员分位本身）。
            normalized = {}
        else:
            weight_sum = sum(weight_map[m] for m in contributions)
            score = sum(contributions.values()) / weight_sum
            # 贡献按**可用成员**的权重和归一：Σcontribution == S，证据面板可直接对账。
            normalized = {
                member: value / weight_sum for member, value in contributions.items()
            }
        collected.append(
            (
                symbol,
                float(score),
                coverage,
                _sample_std(pcts),
                normalized,
                tuple(missing),
            )
        )

    if not collected:
        return BlendResult(
            rows=(),
            member_counts=member_counts,
            stats={
                "n_members": len(ordered_members),
                "n_symbols": len(symbols),
                "n_ranked": 0,
            },
        )

    # 排序：S 降序 → 覆盖降序 → symbol 升序（确定性，无随机）。
    collected.sort(key=lambda item: (-item[1], -item[2], item[0]))
    scores = [item[1] for item in collected]
    score_pcts = compute_rank_pct(scores)

    rows: list[BlendRow] = []
    for index, (
        symbol,
        score,
        coverage,
        dispersion,
        contributions,
        missing,
    ) in enumerate(collected):
        pct = score_pcts[index]
        rows.append(
            BlendRow(
                symbol=symbol,
                score=score,
                rank=index + 1,
                coverage=coverage,
                dispersion=dispersion,
                pct=pct,
                research_score=compute_research_score(pct),
                position=position_of(pct),
                contributions=dict(contributions),
                missing_members=missing,
            )
        )

    stats = _stats(rows, ordered_members, len(symbols), member_counts)
    return BlendResult(rows=tuple(rows), member_counts=member_counts, stats=stats)


def _stats(
    rows: Sequence[BlendRow],
    members: Sequence[str],
    n_symbols: int,
    member_counts: Mapping[str, int],
) -> dict[str, Any]:
    coverages = [row.coverage for row in rows]
    bands = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    positions = {POSITION_BUY: 0, POSITION_HOLD: 0, POSITION_SELL: 0}
    for row in rows:
        bands[dispersion_band(row.dispersion)] += 1
        positions[row.position] = positions.get(row.position, 0) + 1
    return {
        "n_members": len(members),
        "n_available_members": sum(1 for m in members if member_counts.get(m, 0) > 0),
        "n_symbols": n_symbols,
        "n_ranked": len(rows),
        "coverage_mean": (
            float(sum(coverages)) / len(coverages) if coverages else None
        ),
        "dispersion": bands,
        "positions": positions,
    }


def blend_rows_to_dicts(rows: Collection[BlendRow]) -> list[dict[str, Any]]:
    """序列化（保留内部枚举；展示翻译由 API 层/前端完成）。"""
    return [
        {
            "symbol": row.symbol,
            "score": row.score,
            "rank": row.rank,
            "coverage": row.coverage,
            "dispersion": row.dispersion,
            "pct": row.pct,
            "research_score": row.research_score,
            "position": row.position,
            "contributions": dict(row.contributions),
            "missing_members": list(row.missing_members),
        }
        for row in rows
    ]
