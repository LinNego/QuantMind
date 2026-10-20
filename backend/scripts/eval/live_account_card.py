"""实盘账户评分卡（机构级优化批2 · F1）：真实账户 → 四维评分（权重与模拟账户卡同源 30/20/25/25）。

为什么单开一类卡而不是并进账户卡：模拟卡的位置源是 Redis 模拟账户 +
`simulation_fund_snapshots`，实盘的位置源是 PG `real_account_ledger_daily_snapshots`
（日度台账）+ `real_account_snapshots`（盘中快照）——两个世界的「数据缺席」含义完全不同
（模拟空账户=没建仓；实盘缺行=桥/客户端没上报）。混在一张卡里会把「实盘没上报」
误读成「空账户不给分」。留两套卡，各自如实说话，评分口径（四维/权重/阈值）复用
`account_card` 的纯函数，防止两卡漂移。

数据源：
- 台账：PG `real_account_ledger_daily_snapshots` 按**账户家族**读
  （`list_real_account_daily_ledgers_by_family`：跨 user_id 别名与改名前后的账户键，
  与实盘账户页 / 风控档位生产者同一口径），日度 `total_asset/today_pnl` 成净值序列；
- 持仓/现金：PG `real_account_snapshots` 最新可用行，**复用实盘页的按源仲裁**
  （`_fetch_latest_real_account_snapshot`：选定券商优先、空壳行跳过、降级必标注）——
  同一座账户不许两套读法；
- 风控事件：`scripts/eval/risk_events.py`（与模拟卡同一实现）；
- 基准：沪深300（与策略卡 / 模拟账户卡同窗口口径；本卡只评 CN）。

空/缺行语义（**skipped 与 failed 分开**）：
- 台账无行（从未上报，或近窗口无行）→ ``skipped``，不落分，调用方记「跳过」而非「失败」；
- 有台账但持仓快照缺失 → **照常出卡**：暴露维如实缺省并写明原因（不假装空仓、
  不拿台账里的 position_count 顶持仓明细），其余三维照算。

用法：python backend/scripts/eval/live_account_card.py [--user 10000001] [--save] [--json]
      python backend/scripts/eval/live_account_card.py --all --save    # 扫描全部有台账的用户
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.eval.account_card import (  # noqa: E402
    BENCHMARK,
    DEFAULT_WINDOW_DAYS,
    WEIGHTS,
    _pnl_items,
    load_benchmark_closes,
    score_attribution,
    score_capital_efficiency,
    score_exposure,
)
from backend.scripts.eval.account_series import account_series_payload  # noqa: E402
from backend.scripts.eval.risk_events import (  # noqa: E402
    load_risk_summary,
    score_risk_events,
)
from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
)
from backend.shared.eval_series import (  # noqa: E402
    UnsafeObjectId,
    is_series_id,
    save_series,
)
from backend.shared.simulation_account_keys import (  # noqa: E402
    ledger_user_id_candidates,
    normalize_runtime_user,
    resolve_db_account_user,
)
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"
MARKET = "CN"  # 本卡只评 A 股实盘（台账/基准/行业映射全是 CN 口径）


# ── 纯函数（可单测）─────────────────────────────────────────────────


def _num(raw: Any) -> float | None:
    """数值化；None / 非数 / NaN / inf → None（缺测，不是 0）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def normalize_positions(positions: Any) -> dict[str, dict[str, float]]:
    """实盘快照持仓（list[{symbol|stock_code, volume, price, cost_price, market_value}]）
    → ``account_card`` 口径的 dict ``{symbol: {market_value, price, cost, volume}}``。

    - 键先归一到 suffix 形态（``600036.SH``，与行业映射/行情层同口径）；
      认不出的码保持原样，仍进统计（暴露维按市值算，不靠代码格式）；
    - 无代码的行给 ``#序号`` 键——不许多行因为都缺代码被合并成一只；
    - ``volume<=0`` 是桥的清仓残留，剔除（与实盘页口径一致）；缺 market_value
      时按 price×volume 兜底。
    """
    out: dict[str, dict[str, float]] = {}
    if not isinstance(positions, list):
        return out
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            continue
        volume = _num(pos.get("volume"))
        if not volume or volume <= 0:
            continue
        raw_symbol = str(pos.get("symbol") or pos.get("stock_code") or "").strip()
        symbol = StockCodeUtil.to_suffix(raw_symbol) if raw_symbol else f"#{idx}"
        price = _num(pos.get("price")) or 0.0
        cost_raw = pos.get("cost_price")
        if cost_raw is None:
            cost_raw = pos.get("cost")
        cost = _num(cost_raw) or 0.0
        market_value = _num(pos.get("market_value"))
        if (market_value is None or market_value <= 0) and price > 0:
            market_value = price * volume
        out[symbol] = {
            "market_value": float(market_value or 0.0),
            "price": price,
            "cost": cost,
            "volume": volume,
        }
    return out


def _attr(raw: Any, name: str, default: Any = None) -> Any:
    """ORM 行 / dict 双形态取字段（单测用 dict，线上是 ORM 行）。"""
    if isinstance(raw, dict):
        return raw.get(name, default)
    return getattr(raw, name, default)


def ledger_row_dict(raw: Any) -> dict[str, Any]:
    """台账行 → 纯 dict（date/total_asset/today_pnl/…）。

    ``today_pnl`` 口径与实盘页契约构建器逐字一致：
    ``total_asset - day_open_equity``（day_open_equity>0），否则用券商原始
    ``today_pnl_raw``——同一座账户两处数字化不同会显得像 bug。
    """
    total = _num(_attr(raw, "total_asset")) or 0.0
    day_open = _num(_attr(raw, "day_open_equity")) or 0.0
    today_raw = _num(_attr(raw, "today_pnl_raw")) or 0.0
    day = _attr(raw, "snapshot_date")
    return {
        "date": day.isoformat() if hasattr(day, "isoformat") else str(day or ""),
        "total_asset": total,
        "cash": _num(_attr(raw, "cash")) or 0.0,
        "market_value": _num(_attr(raw, "market_value")) or 0.0,
        "day_open_equity": day_open,
        "today_pnl": total - day_open if day_open > 0 else today_raw,
        "initial_equity": _num(_attr(raw, "initial_equity")) or 0.0,
        "position_count": int(_num(_attr(raw, "position_count")) or 0),
    }


# ── IO / 编排 ───────────────────────────────────────────────────────


async def load_live_context(
    tenant: str, user: str, *, days: int = 90
) -> dict[str, Any]:
    """实盘上下文：台账家族序列 + 最新可用持仓快照。

    返回 ``{found, canonical_user, account_id, rows, positions, snapshot,
    snapshot_at, snapshot_source, snapshot_downgraded, snapshot_mismatch,
    snapshot_error, missing_reason}``。``found`` 以**台账有行**为准——
    持仓快照只是暴露维的输入，缺了不叫「账户不存在」。
    """
    from sqlalchemy import desc, select

    from backend.services.trade.services.real_account_ledger_service import (
        account_family,
        list_real_account_daily_ledgers_by_family,
    )
    from backend.services.trade_shared.models.real_account_ledger import (
        RealAccountLedgerDailySnapshot,
    )
    from backend.shared.database_manager_v2 import get_session

    canon = normalize_runtime_user(user)
    ctx: dict[str, Any] = {
        "found": False,
        "canonical_user": canon,
        "account_id": None,
        "rows": [],
        "positions": {},
        "snapshot": None,
        "snapshot_at": None,
        "snapshot_source": None,
        "snapshot_downgraded": None,
        "snapshot_mismatch": None,
        "snapshot_error": None,
        "missing_reason": f"实盘台账无行（{tenant}:{canon} 从未上报过日报台账）",
    }
    if not canon:
        return ctx

    async with get_session(read_only=True) as session:
        anchor = (
            await session.execute(
                select(RealAccountLedgerDailySnapshot.account_id)
                .where(
                    RealAccountLedgerDailySnapshot.tenant_id == tenant,
                    RealAccountLedgerDailySnapshot.user_id.in_(
                        ledger_user_id_candidates(canon)
                    ),
                )
                .order_by(
                    desc(RealAccountLedgerDailySnapshot.snapshot_date),
                    desc(RealAccountLedgerDailySnapshot.last_snapshot_at),
                )
                .limit(1)
            )
        ).scalar()
        if anchor:
            merged = await list_real_account_daily_ledgers_by_family(
                session,
                tenant_id=tenant,
                user_id=canon,
                account_id=str(anchor),
                days=days,
            )
            ctx["account_id"] = str(anchor)
            ctx["rows"] = [ledger_row_dict(r) for r in merged]
            ctx["found"] = bool(ctx["rows"])
            if not ctx["found"]:
                ctx["missing_reason"] = (
                    f"实盘台账近 {days} 天无行（账户 {anchor}）——不拿更早的旧行充数"
                )

        # 最新持仓快照：与实盘账户页同一读取路径（按源仲裁 + 空壳行守卫）
        try:
            from backend.services.live_trading.routers.real_trading_utils import (
                _fetch_latest_real_account_snapshot,
            )

            snapshot = await _fetch_latest_real_account_snapshot(
                session, tenant_id=tenant, user_id=canon
            )
        except Exception as exc:  # noqa: BLE001 - 快照读取失败只降级暴露维
            ctx["snapshot_error"] = f"{type(exc).__name__}: {exc}"
            snapshot = None
        if snapshot is not None and ctx["account_id"]:
            snap_account = str(snapshot.get("account_id") or "")
            if snap_account and account_family(snap_account) != account_family(
                ctx["account_id"]
            ):
                # 同 user 名下有两座账户家族：最新快照可能是另一座的，绝不混用
                ctx["snapshot_mismatch"] = snap_account
                snapshot = None
        if snapshot is not None:
            ctx["snapshot"] = snapshot
            ctx["positions"] = normalize_positions(snapshot.get("positions"))
            ctx["snapshot_at"] = snapshot.get("snapshot_at")
            ctx["snapshot_source"] = snapshot.get("source")
            ctx["snapshot_downgraded"] = snapshot.get("source_downgraded")
    return ctx


async def list_live_users(*, tenant: str = "default") -> list[str]:
    """有实盘台账的用户（规范名去重；别名族 00000001/10000001 归一个）。"""
    from sqlalchemy import select

    from backend.services.trade_shared.models.real_account_ledger import (
        RealAccountLedgerDailySnapshot,
    )
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                select(RealAccountLedgerDailySnapshot.user_id)
                .where(RealAccountLedgerDailySnapshot.tenant_id == tenant)
                .distinct()
            )
        ).scalars()
    users: list[str] = []
    for raw in rows:
        canon = normalize_runtime_user(raw)
        if canon and canon not in users:
            users.append(canon)
    return users


def save_live_sidecar(
    rows: list[dict[str, Any]],
    *,
    combined: dict[str, Any],
    object_id: str,
    live_total_asset: float | None = None,
    n_positions: int | None = None,
) -> dict[str, Any]:
    """实盘台账行 → 长序列侧车（``data/eval_series/live_account/<id>.json``）。

    文案走 ``source_label="实盘"``（模拟/实盘共用载荷函数，样本量警示逐字口径不变）。
    白名单未接线时 ``save_series`` 抛 ``UnsafeObjectId``——如实带回 note，
    不许让侧车写失败把整张卡拖崩。
    """
    if not is_series_id(object_id):
        return {
            "object_id": object_id,
            "written": False,
            "bytes": 0,
            "note": f"object_id 不能作为侧车文件名（{object_id!r}），未写长序列",
        }
    payload = account_series_payload(
        [
            {
                "date": r.get("date"),
                "total_asset": r.get("total_asset"),
                "today_pnl": r.get("today_pnl"),
            }
            for r in rows
        ],
        market=MARKET,
        source_label="实盘",
        scalars={
            "score": combined.get("score"),
            "grade": combined.get("grade"),
            "n_positions": n_positions,
            # 盘中快照总资产（与卡上同源）；台账末行是日度收盘值，两值不同属正常
            "live_total_asset": live_total_asset,
        },
    )
    try:
        sidecar = save_series("live_account", object_id, payload)
    except UnsafeObjectId as exc:
        return {
            "object_id": object_id,
            "written": False,
            "bytes": 0,
            "note": f"侧车类型未接线（{exc}）",
        }
    return {
        "object_id": object_id,
        "written": sidecar["written"],
        "bytes": sidecar["bytes"],
        "note": sidecar["note"],
    }


def _exposure_from_context(ctx: dict[str, Any], total_asset: float, industry_map):
    """暴露维：有快照走正评；无快照如实缺省并写明是哪一种缺。"""
    if ctx["snapshot"] is not None:
        return score_exposure(ctx["positions"], total_asset, industry_map or None)
    if ctx.get("snapshot_mismatch"):
        note = (
            f"最新快照属于另一座账户（{ctx['snapshot_mismatch']}），不混用"
            "——暴露维如实缺省"
        )
    elif ctx.get("snapshot_error"):
        note = f"实盘持仓快照读取失败（{ctx['snapshot_error']}）——暴露维如实缺省"
    else:
        note = "实盘持仓快照缺失（real_account_snapshots 无可用行）——暴露维如实缺省"
    return DimensionScore(
        "exposure",
        "风险暴露",
        WEIGHTS["exposure"],
        None,
        False,
        {"insufficient": True, "note": note},
    )


async def score_live_account(
    user: str,
    *,
    tenant: str = "default",
    window_days: int = DEFAULT_WINDOW_DAYS,
    save: bool = False,
) -> dict[str, Any]:
    """单实盘账户四维评分：台账序列 + 最新快照持仓 + 风控事件 + 基准。"""
    canon = normalize_runtime_user(user)
    object_id = f"{canon}:{MARKET}"
    ctx = await load_live_context(tenant, user, days=max(90, window_days * 3))
    if not ctx["found"]:
        return {
            "object_type": "live_account",
            "object_id": object_id,
            "skipped": True,
            "error": ctx["missing_reason"],
        }

    latest = ctx["rows"][-1]
    snapshot = ctx["snapshot"]
    if snapshot is not None:
        total_asset = _num(snapshot.get("total_asset")) or 0.0
        cash = _num(snapshot.get("available_cash"))
        market_value = _num(snapshot.get("market_value")) or 0.0
    else:
        total_asset = float(latest.get("total_asset") or 0.0)
        cash = latest.get("cash")
        market_value = float(latest.get("market_value") or 0.0)

    positions = ctx["positions"]
    series: list[tuple[str, float]] = [
        (str(r["date"]), float(r["total_asset"] or 0.0)) for r in ctx["rows"]
    ]
    bench: list[tuple[str, float]] = []
    if len(series) >= 3:
        bench = load_benchmark_closes(series[0][0], series[-1][0])

    industry_map: dict[str, str] = {}
    try:
        from backend.services.engine.inference.shenwan_industry import (
            load_shenwan_industry_map,
        )

        industry_map = load_shenwan_industry_map()
    except Exception:  # noqa: BLE001 - 行业映射缺失只降级行业子项
        industry_map = {}

    risk_summary = await load_risk_summary(tenant, canon, days=window_days)

    dims = [
        _exposure_from_context(ctx, total_asset, industry_map),
        score_attribution(series, bench, _pnl_items(positions)),
        score_risk_events(risk_summary),
        score_capital_efficiency(
            total_asset, float(cash) if cash is not None else None, market_value
        ),
    ]
    combined = combine_dimension_scores(dims)
    sidecar = save_live_sidecar(
        ctx["rows"],
        combined=combined,
        object_id=object_id,
        live_total_asset=round(total_asset, 2) if total_asset > 0 else None,
        n_positions=len(positions),
    )
    result: dict[str, Any] = {
        "object_type": "live_account",
        "object_id": object_id,
        "tenant": tenant,
        "user": canon,
        "account_id": ctx["account_id"],
        "total_asset": round(total_asset, 2),
        "n_positions": len(positions) if snapshot is not None else None,
        "window": [series[0][0], series[-1][0]] if series else None,
        "snapshot_window_days": window_days,
        "inputs_version": {
            "weights": WEIGHTS,
            "benchmark": BENCHMARK,
            "industry_map": bool(industry_map),
            "position_source": (
                f"real_account_snapshot:{ctx.get('snapshot_source')}"
                if snapshot is not None
                else None
            ),
            "snapshot_at": ctx.get("snapshot_at"),
            "snapshot_downgraded": ctx.get("snapshot_downgraded"),
            "ledger_account_id": ctx["account_id"],
            "ledger_points": len(ctx["rows"]),
            # 长序列走 `data/eval_series/live_account/<用户>%3ACN.json`；
            # 台账点少/写失败都由载荷 note 如实带出（5 个点不是趋势）
            "series_sidecar": sidecar,
        },
        **combined,
    }
    if save and combined.get("score") is not None:
        from backend.shared.eval_contract import save_eval_score

        await save_eval_score(
            object_type="live_account",
            object_id=object_id,
            snapshot_date=date.today(),
            score=combined.get("score"),
            grade=combined.get("grade"),
            low_confidence=bool(combined.get("low_confidence")),
            red_line_failed=combined.get("red_line_failed") or [],
            dimensions=combined.get("dimensions") or {},
            inputs_version=result["inputs_version"],
            tenant_id=tenant,
        )
        result["saved"] = True
    return result


async def score_all_live_accounts(
    *,
    tenant: str = "default",
    window_days: int = DEFAULT_WINDOW_DAYS,
    save: bool = False,
) -> list[dict[str, Any]]:
    """扫描全部有台账的用户逐一评分（单账户异常隔离，不拖垮其余）。"""
    try:
        users = await list_live_users(tenant=tenant)
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "object_type": "live_account",
                "object_id": "*",
                "error": f"实盘台账扫描失败: {exc}",
            }
        ]
    results: list[dict[str, Any]] = []
    for user in users:
        try:
            results.append(
                await score_live_account(
                    user, tenant=tenant, window_days=window_days, save=save
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单账户隔离
            results.append(
                {
                    "object_type": "live_account",
                    "object_id": f"{user}:{MARKET}",
                    "error": f"评分失败: {exc}",
                }
            )
    return results


def render_card(result: dict[str, Any]) -> str:
    if result.get("error"):
        kind = "跳过" if result.get("skipped") else "错误"
        return f"实盘账户卡 {result.get('object_id')}（{kind}）：{result['error']}"
    lines = [
        f"实盘账户评分卡 {result['object_id']}：{result.get('score')} 分 ｜评级 {result.get('grade')}"
        f"（总资产 {result.get('total_asset')}，持仓 {result.get('n_positions')} 只）"
        + ("（低置信 †）" if result.get("low_confidence") else ""),
        "─" * 46,
    ]
    for key, dim in (result.get("dimensions") or {}).items():
        score = dim.get("score")
        lines.append(
            f"{dim.get('label')}({key}): {score if score is not None else '缺省'} × {dim.get('weight')}"
            + ("  ⚠红线" if dim.get("red_line_failed") else "")
        )
    if result.get("missing_dims"):
        lines.append(f"缺省维度（权重归一）: {result['missing_dims']}")
    return "\n".join(lines)


async def _main_async(args) -> list[dict[str, Any]]:
    if args.all:
        return await score_all_live_accounts(
            tenant=args.tenant, window_days=args.window, save=args.save
        )
    return [
        await score_live_account(
            args.user, tenant=args.tenant, window_days=args.window, save=args.save
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="实盘账户评分卡（机构级批2 · F1）")
    parser.add_argument(
        "--user",
        default=resolve_db_account_user(ENV_ACCOUNT_USER),
        help=f"实盘账户 user（默认与决策轮同源：resolve_db_account_user({ENV_ACCOUNT_USER})）",
    )
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--all", action="store_true", help="扫描全部有台账的用户")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS, help="风控事件窗口天数"
    )
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    async def _run():
        from backend.shared.database_manager_v2 import close_database

        try:
            return await _main_async(args)
        finally:
            await close_database()

    results = asyncio.run(_run())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(render_card(r))
    return 0 if any(not r.get("error") for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
