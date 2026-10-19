"""账户/分账账本的**只读契约**（P2.7 / 2026-10-19 额度口径）。

两个 DTO 从 ``decision_round_core`` 拆出，原因有两条：

1. **文件预算**（每模块 < 800 行）：core 顶到 840 行，而这两个类与「一轮怎么跑」
   无关，只是外界账本的形状；
2. **口径唯一性**：提示词额度只认 :attr:`AgentLedgerRead.quota_total`（用户授予的
   名义额度），真实账户的 :attr:`AccountRead.quota_total` **仅供内部风控/审计**。
   两个 DTO 放在一起，这条边界才是看得见、可审查的。

本模块是**叶子**：只 import shared（``agent_ledger``），不 import 任何
``decision_round_*`` 模块（依赖方向见 ``test_decision_round.py`` 的反向依赖表）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from backend.shared.decision.agent_ledger import DEFAULT_AGENT_QUOTA, positions_cost


@dataclass(frozen=True, slots=True)
class AccountRead:
    """实盘账户额度三数（**同一行快照**：cash / market_value / total_asset）。"""

    ok: bool = False
    cash: float | None = None
    market_value: float | None = None
    total_asset: float | None = None
    source: str = ""
    #: 读这份快照时选定的券商（``broker:selected:CN``）——两座真实账户差 ~25 倍，
    #: 复盘必须能看出「这一轮的额度是从谁的账上读的」。审计 ``context_meta.account``
    #: 带上它（读失败/未选定时为空串）。
    broker: str = ""
    snapshot_at: str = ""
    age_min: float | None = None
    errors: tuple[str, ...] = ()

    @property
    def quota_total(self) -> float:
        """真实账户总额 = cash + market_value。

        **仅供内部风控/审计，禁止进入模型提示词**（2026-10-19 用户拍板：agent
        只能动用授予额度，不得看见账户总金额）。提示词额度一律取
        :attr:`AgentLedgerRead.quota_total`。
        """
        return round(float(self.cash or 0.0) + float(self.market_value or 0.0), 2)

    @property
    def quota_used(self) -> float:
        """真实账户持仓市值。**仅供内部风控/审计，禁止进入模型提示词。**"""
        return round(float(self.market_value or 0.0), 2)


@dataclass(frozen=True, slots=True)
class AgentLedgerRead:
    """本 agent 在**分账账本**里的那一段（P2.7）：名下持仓 + 子账户虚拟现金。

    ``ok=False`` 只有一个来路——**读失败**（DB 不可用/表缺失）。调用点必须中止本轮：
    看不到自己的账 ⇒ 既不知道哪些票是自己的（卖错票正是 2026-09-08 那次事故），
    也不知道自己还剩多少额度。

    与「读到了，但账本里查无此 agent」是**两件事**，后者是 ``ok=True`` +
    ``known=False``：语义是「这家模型名下没有货」——``positions`` 是空集，**不是**
    「没配分账就全给我」。那一句兜底（``if mine else holdings``）正是事故的成因，
    故本 DTO 里**没有**「回退到全账户持仓」这个选项可以选。
    """

    ok: bool = False
    #: 账本里有没有这家 agent（有账户行或持仓行）。``False`` = 从未在这本账里成交过。
    known: bool = False
    #: ``{后缀码: {volume, cost_price, buy_ts, last_ts}}``——与 ``HoldingRow.code`` 同形态。
    positions: Mapping[str, Any] = field(default_factory=dict)
    #: 子账户虚拟现金（元）。账本无此行 = 满额 :data:`DEFAULT_AGENT_QUOTA`。
    virtual_cash: float = DEFAULT_AGENT_QUOTA
    errors: tuple[str, ...] = ()

    def mine(self) -> frozenset[str]:
        """本 agent 名下的后缀码集合（``mine_of`` 的口径：**空集就是空集**）。"""
        return frozenset(self.positions)

    @property
    def quota_total(self) -> float:
        """用户授予的额度（名义值）。真实账户总额仅供内部风控，**禁止入提示词**。"""
        return DEFAULT_AGENT_QUOTA

    @property
    def quota_used(self) -> float:
        """已用额度 = 名下持仓成本合计（与 ``agent_used`` 同口径；卖出即释放）。"""
        return positions_cost(self.positions)

    @property
    def quota_remaining(self) -> float:
        """剩余授予额度 = ``quota_total − quota_used``（超用如实为负，不夹 0）。"""
        return round(self.quota_total - self.quota_used, 2)


__all__ = ["AccountRead", "AgentLedgerRead"]
