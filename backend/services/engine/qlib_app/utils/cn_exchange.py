import json
import logging
import threading
from datetime import date, datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, OrderDir
from qlib.backtest.exchange import Exchange
from qlib.backtest.position import BasePosition

from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "CnExchange")

# 涨跌停判定的取整容差不再在本模块持有数值：唯一事实源 =
# ``local_market_data.LIMIT_TOLERANCE``（0.5pp），用时在函数内导入。
# 本模块曾有 `_LIMIT_TOLERANCE = 0.005` 的独立字面量，与另 4 处同值副本并存 ——
# 同值不代表同源，下一次改口径只会改到一份。`$change` 是四舍五入后的日收益，
# 涨跌停价本身又按分取整，两处取整都由该容差吸收。


def _as_trade_date(value: object) -> date | None:
    """把 qlib 传进来的时间戳收敛成 ``date``；认不出来时返回 None。

    返回 None 而非抛异常：调用方会退化成「按当日板规」。这条降级路径在
    **当日**检查（盘后风控、实盘下单前的封板校验）上是正确的，只有回测历史
    窗口才可能因板规差异判错，所以降级要留痕（见调用点的 warning 日志）。
    """
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return None


class CnExchange(Exchange):
    """
    Custom Qlib Exchange for China A-Shares.
    Implements specific cost rules:
    - Commission: Rate + Min (e.g. 0.025%, min 5)
    - Stamp Duty: Sell only (e.g. 0.05%)
    - Transfer Fee: SH only (e.g. 0.001%, min 0.01)
    - Market Impact: Slippage based on volume participation (Square Root Law)
    """

    def __init__(
        self,
        commission: float = 0.00025,
        min_commission: float = 5.0,
        stamp_duty: float = 0.0005,
        transfer_fee: float = 0.00001,
        min_transfer_fee: float = 0.01,
        impact_cost_coefficient: float = 0.0005,  # 市场冲击成本系数（与 Schema 默认值保持一致）
        backtest_id: str | None = None,  # 新增 backtest_id 用于关联交易记录
        allow_short_selling: bool = False,
        max_participation_rate: float | None = None,  # 单笔成交占当日量上限（默认 5%，防 75% 一口吃）
        has_price_limits: bool = True,  # HK/US 等无涨跌停市场传 False（防未来补 change 数据后被误拦）
        stamp_duty_on_sell_only: bool = True,  # 港股印花按卖出单边计提简化（与模拟盘 HK_RULES 同口径）
        **kwargs,
    ):
        # Pass dummy costs to super because we will calculate our own
        super().__init__(open_cost=0, close_cost=0, min_cost=0, **kwargs)
        self.commission = commission
        self.min_commission = min_commission
        self.stamp_duty = stamp_duty
        self.transfer_fee = transfer_fee
        self.min_transfer_fee = min_transfer_fee
        self.impact_cost_coefficient = impact_cost_coefficient
        self.backtest_id = backtest_id
        self.allow_short_selling = bool(allow_short_selling)
        if max_participation_rate is None:
            try:
                import os

                max_participation_rate = float(os.getenv("QLIB_MAX_PARTICIPATION_RATE", "0.05"))
            except Exception:
                max_participation_rate = 0.05
        # <=0 关闭该硬约束；非有限值同样视为关闭，避免 NaN 把成交全截成 0
        rate = float(max_participation_rate)
        self.max_participation_rate = rate if np.isfinite(rate) and rate > 0 else 0.0
        self.has_price_limits = bool(has_price_limits)
        self.stamp_duty_on_sell_only = bool(stamp_duty_on_sell_only)

        # Redis client for logging trades
        self.redis_client = None
        self.quote_fallback_lookback_days = 10
        try:
            import os

            raw_lookback = os.getenv("QLIB_QUOTE_FALLBACK_LOOKBACK_DAYS", "10")
            self.quote_fallback_lookback_days = max(1, int(raw_lookback))
        except Exception:
            self.quote_fallback_lookback_days = 10
        if self.backtest_id:
            try:
                self.redis_client = get_redis_sentinel_client()
                self.trades_key = f"qlib:backtest:trades:{self.backtest_id}"
                # Set expire time for the key (e.g., 24 hours)
                self.redis_client.expire(self.trades_key, 86400)
            except Exception as e:
                task_logger.warning("redis_client_init_failed", "Failed to initialize Redis client in CnExchange", error=str(e))

    @staticmethod
    def _is_invalid_quote(value: object) -> bool:
        if value is None:
            return True
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return True
        return np.isnan(numeric) or np.isinf(numeric) or numeric <= 1e-8

    def _get_recent_valid_quote(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        field: str,
        method: str = "ts_data_last",
    ) -> float | None:
        value = self.quote.get_data(stock_id, start_time, end_time, field=field, method=method)
        if not self._is_invalid_quote(value):
            return float(value)

        for offset in range(1, self.quote_fallback_lookback_days + 1):
            prev_start = pd.Timestamp(start_time) - pd.Timedelta(days=offset)
            prev_end = pd.Timestamp(end_time) - pd.Timedelta(days=offset)
            fallback_value = self.quote.get_data(stock_id, prev_start, prev_end, field=field, method=method)
            if self._is_invalid_quote(fallback_value):
                continue

            task_logger.warning(
                "quote_fallback_applied",
                "Quote fallback applied with previous valid data",
                stock_id=stock_id,
                field=field,
                requested_start=str(pd.Timestamp(start_time)),
                requested_end=str(pd.Timestamp(end_time)),
                fallback_start=str(prev_start),
                fallback_end=str(prev_end),
                fallback_value=float(fallback_value),
                lookback_days=offset,
            )
            return float(fallback_value)

        return None if self._is_invalid_quote(value) else float(value)

    def get_close(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        method: str = "ts_data_last",
    ) -> float | None:
        return self._get_recent_valid_quote(stock_id, start_time, end_time, field="$close", method=method)

    def get_factor(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
    ) -> float | None:
        if stock_id not in self.quote.get_all_stock():
            return None
        return self._get_recent_valid_quote(stock_id, start_time, end_time, field="$factor", method="ts_data_last")

    def get_deal_price(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        direction: OrderDir,
        method: str | None = "ts_data_last",
    ) -> float | None:
        if direction == OrderDir.SELL:
            pstr = self.sell_price
        elif direction == OrderDir.BUY:
            pstr = self.buy_price
        else:
            raise NotImplementedError("This type of input is not supported")

        deal_price = self._get_recent_valid_quote(stock_id, start_time, end_time, field=pstr, method=method or "ts_data_last")
        if method is not None and self._is_invalid_quote(deal_price):
            task_logger.warning(
                "deal_price_fallback_to_close",
                "Invalid deal price, falling back to close price",
                stock_id=stock_id,
                field=pstr,
                start_time=str(pd.Timestamp(start_time)),
                end_time=str(pd.Timestamp(end_time)),
            )
            deal_price = self.get_close(stock_id, start_time, end_time, method)
        return deal_price

    def deal_order(
        self,
        order: Order,
        trade_account: BasePosition | None = None,
        force_deal: bool = False,
        position: BasePosition | None = None,
        dealt_order_amount: dict | None = None,
    ) -> tuple[float, float, float]:
        """
        Override deal_order to log trades to Redis
        """
        # Call super method to execute the trade
        # Qlib Exchange.deal_order signature:
        # def deal_order(self, order, trade_account=None, position=None, dealt_order_amount=None)

        # NOTE: 'force_deal' is NOT in standard Qlib Exchange.deal_order
        # It seems it was added in our custom logic or passed by a custom executor?
        # If standard executor passes it, it might be in **kwargs but executor.py line 604
        # shows explicit arguments? No, executor usually calls with specific args.
        # The traceback showed:
        # trade_val, trade_cost, trade_price = self.trade_exchange.deal_order(
        # TypeError: CnExchange.deal_order() got an unexpected keyword argument 'dealt_order_amount'

        # So we MUST accept dealt_order_amount.
        # And when we call super(), we should pass what parent expects.

        # Parent expects: order, trade_account, position, dealt_order_amount
        # It does NOT expect force_deal.

        trade_val, trade_cost, trade_price = super().deal_order(
            order, trade_account=trade_account, position=position, dealt_order_amount=dealt_order_amount
        )

        # 只记**真实成交**：`trade_account` 非空才写台账流水。
        #
        # 为什么必须区分（2026-10-13 复现证实）：`deal_order` 在一步回测里会被
        # 调多次，其中**只有一次是真实成交**——
        #   1) `TopkDropoutStrategy.generate_trade_decision`（signal_strategy.py:258）
        #      对每个**卖单**调 `deal_order(sell_order, position=current_temp)`，
        #      用 deepcopy 的持仓估算「可动用现金」——这不是成交；
        #   2) `SimulatorExecutor._collect_data`（executor.py:604）才用
        #      `trade_account=self.trade_account` 真正撮合。
        # 买入腿不调 `deal_order`（只 get_deal_price/get_factor），所以旧实现在
        # 线上只把**卖单**写了两遍（52 键 8.1 万行里 2.5 万重复，方向分布
        # {'sell': 24940}，买组 0，两份仅 timestamp 差 ~8 ms）。
        # 下游 `RiskAnalyzer._deduplicate_trades` 一直在替它擦屁股——
        # 那是兜底，不该是主防线。
        if self.redis_client and trade_val > 0 and trade_account is not None:
            try:
                action = "buy" if order.direction == OrderDir.BUY else "sell"
                trade_record = {
                    "date": order.start_time.strftime("%Y-%m-%d"),
                    "symbol": str(order.stock_id),
                    "action": action,
                    "price": float(trade_price),
                    "quantity": float(order.deal_amount),
                    "amount": float(trade_val),
                    "commission": float(trade_cost),
                    "timestamp": datetime.now().isoformat(),
                }
                self.redis_client.rpush(self.trades_key, json.dumps(trade_record))
            except Exception:
                # Suppress errors to avoid interrupting backtest
                pass

        return trade_val, trade_cost, trade_price

    def calculate_cost(
        self,
        stock_id: str,
        trade_val: float,
        direction: int,
        market_volume_val: float = 0.0,
    ) -> float:
        """
        Calculate transaction cost
        :param market_volume_val: Total market volume value (price * volume) for the day
        """
        if trade_val <= 1e-5:
            return 0.0

        # 1. Commission (Buy & Sell)
        comm = max(trade_val * self.commission, self.min_commission)

        # 2. Transfer Fee (Buy & Sell, SH only)
        tf = 0.0
        if stock_id.upper().startswith("SH"):
            tf = max(trade_val * self.transfer_fee, self.min_transfer_fee)

        # 3. Stamp Duty（A 股仅卖出；港股简化同口径卖出单边计提）
        tax = 0.0
        if not self.stamp_duty_on_sell_only or direction == OrderDir.SELL:
            tax = trade_val * self.stamp_duty

        # 4. Market Impact Cost (Slippage)
        # Slippage approx = Coeff * sqrt(TradeValue / MarketVolumeValue)
        impact = 0.0
        if market_volume_val > 0:
            participation_rate = trade_val / market_volume_val
            # 采用平方根定律模拟冲击成本
            impact = trade_val * self.impact_cost_coefficient * np.sqrt(participation_rate)
            if participation_rate > 0.1:  # 参与率超过 10% 时额外警告
                task_logger.debug(
                    "high_participation_rate",
                    "High participation rate",
                    stock_id=stock_id,
                    participation_rate=f"{participation_rate:.2%}",
                )

        return comm + tf + tax + impact

    def _get_max_buy_amount(
        self,
        stock_id: str,
        trade_price: float,
        cash: float,
        market_volume_val: float = 0.0,
    ) -> float:
        """Calculate max buy amount given cash limit"""
        if cash <= 0:
            return 0.0

        is_sh = stock_id.upper().startswith("SH")
        rate_sum = self.commission
        if is_sh:
            rate_sum += self.transfer_fee

        # Initial estimate
        amount = cash / (trade_price * (1 + rate_sum))

        # Verify and adjust (Iterative approach to account for fixed costs and market impact)
        for _ in range(3):
            val = amount * trade_price
            cost = self.calculate_cost(stock_id, val, OrderDir.BUY, market_volume_val=market_volume_val)
            if val + cost <= cash:
                break
            else:
                ratio = cash / (val + cost)
                amount = amount * ratio

        return amount

    #: PIT ST 稀疏表的进程内缓存：``dict[后缀式代码, 升序 YYYYMMDD int 数组]``。
    #: ``_ST_PIT_LOADED=False`` 表示尚未尝试加载；加载失败/表缺失时缓存 None。
    _ST_PIT_LOADED: bool = False
    _ST_PIT_LOOKUP: dict[str, np.ndarray] | None = None
    _ST_PIT_LOCK = threading.Lock()

    @classmethod
    def _load_st_pit(cls) -> dict[str, np.ndarray] | None:
        """加载 PIT ST 稀疏表（``symbol,time,is_st``，只存 True 行）。

        数据面由 ``backend/scripts/build_st_pit.py`` 生成（baostock 逐日 isST）。
        表缺失/加载失败 → None（调用方按非 ST 处理），**绝不**回退到
        ``instrument_detail.IsSTGP`` 静态快照 —— 快照回答的是「现在是不是
        ST」，套到历史就是前视（2026-10-22 审计：预测 top decile 的 ST 占比
        8.5% vs 全市场 4.0%）。
        """
        with cls._ST_PIT_LOCK:
            if cls._ST_PIT_LOADED:
                return cls._ST_PIT_LOOKUP
            cls._ST_PIT_LOADED = True
            lookup: dict[str, np.ndarray] | None = None
            try:
                from backend.services.engine.factor_research.data import load_st_pit

                table = load_st_pit("CN")
                if table is not None and not table.empty:
                    frame = pd.DataFrame(
                        {
                            "symbol": table["symbol"].astype(str),
                            "time": pd.to_numeric(
                                table["time"].astype(str), errors="coerce"
                            ),
                        }
                    ).dropna()
                    lookup = {
                        str(symbol): np.sort(group["time"].to_numpy(dtype=np.int64))
                        for symbol, group in frame.groupby("symbol", sort=False)
                    }
            except Exception:
                logger.warning(
                    "PIT ST 表加载失败，涨跌停判定按非 ST 处理", exc_info=True
                )
                lookup = None
            cls._ST_PIT_LOOKUP = lookup
            return lookup

    @classmethod
    def _resolve_is_st(cls, stock_id: str, trade_date: date | None = None) -> bool:
        """该标的在 ``trade_date`` 当日是否 ST（PIT 口径，无前视）。

        稀疏表未命中 = 非 ST；表缺失 = 非 ST（保持「不误杀」的乐观口径）。
        """
        lookup = cls._load_st_pit()
        if not lookup:
            return False
        days = lookup.get(StockCodeUtil.to_suffix(stock_id))
        if days is None or days.size == 0:
            return False
        key = int(pd.Timestamp(trade_date or date.today()).strftime("%Y%m%d"))
        pos = int(np.searchsorted(days, key))
        return pos < days.size and int(days[pos]) == key

    @staticmethod
    def _get_limit_threshold(
        stock_id: str,
        trade_date: date | None = None,
        *,
        is_st: bool | None = None,
    ) -> float:
        """该标的在 ``trade_date`` 当日的涨跌停阈值（含取整容差）。

        口径**唯一事实源** = ``local_market_data.limit_pct``：板块前缀
        （300/301/302、688/689、北交所 43/83/87/88/92）、创业板 2020-08-24
        注册制改革、ST 主板 5%→10% 切换全在那里，本函数不再自持任何板规常量。

        旧实现按代码前缀直接返回 0.095/0.195/0.295，对改革**前**的创业板
        一律给 20%，于是 +9.99% 的真涨停被判成「可交易」——回测里表现为在
        涨停价上成交。本函数现在的契约是「恒等于权威口径减容差」。

        ST 口径：``is_st=None``（默认）时按 PIT 稀疏表逐日判定 —— 无前视；
        显式传 True/False 的调用方（测试、外部覆盖）优先。

        唯一**显式**缺口：0.5pp 容差是启发式，用来吸收 $change 的舍入与
        涨跌停价的按分取整。真要做到精确，得用 ``compute_limits`` 算出
        涨跌停价再比价位。
        """
        from backend.services.simulation.services.local_market_data import (
            limit_threshold,
        )

        day = trade_date or date.today()
        if is_st is None:
            is_st = CnExchange._resolve_is_st(stock_id, day)
        return limit_threshold(stock_id, is_st=is_st, trade_date=day)

    def check_stock_limit(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        direction: int | None = None,
    ) -> bool:
        """Override base check_stock_limit to compute limits from $change field.

        The base class reads $limit_buy/$limit_sell which don't exist in our
        Qlib bin data.  Instead we read the $change field (daily return) and
        compare against the board-specific limit threshold.

        Returns True if the stock is at price limit (NOT tradable).
        """
        if not self.has_price_limits:
            # 无涨跌停市场（HK/US 等）：永远可交易
            return False
        try:
            change = self.quote.get_data(
                stock_id, start_time, end_time, field="$change", method="ts_data_last"
            )
            if change is None or np.isnan(float(change)):
                return False

            change = float(change)
            # 用 signal 日而非「今天」定板规：创业板注册制改革两侧阈值不同。
            trade_date = _as_trade_date(start_time)
            if trade_date is None:
                logger.warning(
                    "涨跌停板规回退到当日：无法从 start_time 解析交易日 "
                    "(stock_id=%s, start_time=%r)，历史窗口可能因板规差异判错",
                    stock_id,
                    start_time,
                )
            threshold = self._get_limit_threshold(stock_id, trade_date)

            if direction is None:
                # Any limit → not tradable
                return abs(change) >= threshold
            elif direction == OrderDir.BUY:
                # Limit-UP → cannot buy
                return change >= threshold
            elif direction == OrderDir.SELL:
                # Limit-DOWN → cannot sell
                return change <= -threshold
            else:
                return False
        except Exception:
            # 读不到 $change 就不拦单 —— 这是**乐观**方向的降级（放行可能已封板的
            # 委托）。刻意保留该行为以免改变既有回测结论，但必须留痕，否则将来
            # 「阈值算错了」和「数据读不到」在日志里长得一模一样。
            logger.exception("涨跌停判定失败，按可交易放行 (stock_id=%s)", stock_id)
            return False

    def quote_clipping(self, order: Order) -> Order | None:
        """
        Clip the order based on price limits.
        Uses our overridden check_stock_limit that reads $change data.
        """
        if self.check_stock_limit(order.stock_id, order.start_time, order.end_time, direction=order.direction):
            task_logger.info("skip_trade_by_limit", "Skip trade by price limit", stock_id=order.stock_id, start_time=str(order.start_time))
            order.deal_amount = 0.0
            return order

        return order

    def _clip_amount_by_participation(self, order: Order, market_volume: float | None) -> None:
        """单笔成交不超过当日成交量的 ``max_participation_rate``（默认 5%）。

        旧口径只把参与率塞进冲击成本（>10% 仅 warning），75% 一口吃仍全额成交；
        这里直接截断，余量视为未成交缺口。``max_participation_rate<=0`` 关闭。
        """
        try:
            rate = float(self.max_participation_rate)
            if rate <= 0 or market_volume is None:
                return
            volume = float(market_volume)
            if not np.isfinite(volume) or volume <= 0:
                return
            factor = float(order.factor) if order.factor is not None else 1.0
            if not np.isfinite(factor) or factor <= 0:
                factor = 1.0
            # 单位对齐：$volume 是**真实股数**（qlib_data_builder 不调整 volume），
            # 而 deal_amount 是**复权股数**（real_shares = deal_amount × factor）。
            # 先换算到复权口径，否则 factor>1 时会按 factor 倍放行。
            cap = volume * rate / factor
            if order.deal_amount <= cap:
                return
            capped = cap
            try:
                # 与其它裁剪路径同口径：按整手向下取整
                capped = float(self.round_amount_by_trade_unit(cap, factor))
            except Exception:
                capped = cap
            capped = max(min(capped, float(order.deal_amount)), 0.0)
            task_logger.info(
                "clip_by_participation",
                "Clip order by participation cap",
                stock_id=str(order.stock_id),
                start_time=str(order.start_time),
                requested=float(order.deal_amount),
                capped=float(capped),
                market_volume=float(volume),
                factor=float(factor),
                max_participation_rate=rate,
            )
            order.deal_amount = capped
        except Exception:
            logger.debug("参与率裁剪失败，保留原成交量", exc_info=True)

    def _calc_trade_info_by_order(
        self,
        order: Order,
        position: BasePosition | None,
        dealt_order_amount: dict,
    ) -> tuple[float, float, float]:
        """
        Calculation of trade info
        **NOTE**: Order will be changed in this function
        """
        trade_price = self.get_deal_price(order.stock_id, order.start_time, order.end_time, direction=order.direction)
        if trade_price is None or np.isnan(trade_price) or trade_price <= 0:
            return 0.0, 0.0, 0.0

        trade_price = float(trade_price)

        # 获取市场当日成交量 (用于计算冲击成本)
        market_volume = self.get_volume(order.stock_id, order.start_time, order.end_time)
        market_volume_val = float(market_volume) * trade_price if market_volume is not None else 0.0

        # Basic volume clipping
        order.factor = self.get_factor(order.stock_id, order.start_time, order.end_time)
        order.deal_amount = order.amount
        self._clip_amount_by_volume(order, dealt_order_amount)

        # 容量硬约束：单笔不超过当日量 max_participation_rate（默认 5%）
        self._clip_amount_by_participation(order, market_volume)

        # Cash / Position limit clipping
        if order.direction == Order.SELL:
            if position is not None:
                if self.allow_short_selling:
                    order.deal_amount = self.round_amount_by_trade_unit(
                        order.deal_amount,
                        order.factor,
                    )
                else:
                    current_amount = (
                        position.get_stock_amount(order.stock_id) if position.check_stock(order.stock_id) else 0
                    )
                    if not np.isclose(order.deal_amount, current_amount):
                        order.deal_amount = self.round_amount_by_trade_unit(
                            min(current_amount, order.deal_amount),
                            order.factor,
                        )

                    trade_val = order.deal_amount * trade_price
                    trade_cost = self.calculate_cost(
                        order.stock_id,
                        trade_val,
                        Order.SELL,
                        market_volume_val=market_volume_val,
                    )
                    if position.get_cash() + trade_val < trade_cost:
                        order.deal_amount = 0

        elif order.direction == Order.BUY:
            if position is not None:
                if self.allow_short_selling:
                    # Margin limits are enforced by strategy level weights and account constraints.
                    # We bypass strict cash limitation here to allow borrowing cash (negative cash).
                    order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)
                else:
                    cash = position.get_cash()
                    trade_val = order.deal_amount * trade_price
                    trade_cost = self.calculate_cost(
                        order.stock_id,
                        trade_val,
                        Order.BUY,
                        market_volume_val=market_volume_val,
                    )

                    if cash < trade_cost:
                        order.deal_amount = 0
                    elif cash < trade_val + trade_cost:
                        max_amount = self._get_max_buy_amount(
                            order.stock_id,
                            trade_price,
                            cash,
                            market_volume_val=market_volume_val,
                        )
                        order.deal_amount = self.round_amount_by_trade_unit(
                            min(max_amount, order.deal_amount), order.factor
                        )
                    else:
                        order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)
            else:
                order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)

        # Final Calculation
        trade_val = order.deal_amount * trade_price
        trade_cost = self.calculate_cost(
            order.stock_id,
            trade_val,
            order.direction,
            market_volume_val=market_volume_val,
        )

        return trade_price, trade_val, trade_cost
