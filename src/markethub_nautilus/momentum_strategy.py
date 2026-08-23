from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from .china_rules import AShareRuleBook, calculate_fee
from .config import Decision, StrategySpec
from .momentum import RankedDecision, canonical_weight

SHANGHAI = ZoneInfo("Asia/Shanghai")
VENUE = Venue("XCN")


class MomentumTopKStrategy(Strategy):
    """Computes momentum from observed bars and executes target weights at next open."""

    def __init__(
        self,
        spec: StrategySpec,
        trading_days: tuple[date, ...],
        bar_types: tuple[BarType, ...],
        canonical_by_native: dict[str, str],
        native_by_canonical: dict[str, object],
        rule_book: AShareRuleBook,
        suspended_keys: frozenset[tuple[date, str]],
    ) -> None:
        super().__init__()
        self.spec = spec
        self.trading_days = trading_days
        self.bar_types = bar_types
        self.canonical_by_native = canonical_by_native
        self.native_by_canonical = native_by_canonical
        self.rule_book = rule_book
        self.suspended_keys = suspended_keys
        self._history: dict[str, list[tuple[date, Decimal]]] = {
            instrument: [] for instrument in native_by_canonical
        }
        self._signal_alert_days: set[date] = set()
        self._processed_signal_days: set[date] = set()
        self._seen_by_day: dict[date, set[str]] = {}
        self._decisions_by_day: dict[date, list[RankedDecision]] = {}
        self._pending_by_alert: dict[str, tuple[date, date]] = {}
        self._pending_buys: dict[
            str, tuple[date, date, dict[str, int], dict[str, RankedDecision]]
        ] = {}
        self.runtime_decisions: list[RankedDecision] = []
        self.decision_records: list[dict[str, str]] = []
        self.position_quantity = {instrument: 0 for instrument in native_by_canonical}
        self.acquired_day: dict[str, date] = {}
        self.order_records: list[dict[str, object]] = []
        self.reject_records: list[dict[str, object]] = []
        self.fill_records: list[dict[str, object]] = []
        self.position_records: list[dict[str, object]] = []
        self.fee_records: list[dict[str, object]] = []

    def on_start(self) -> None:
        for bar_type in self.bar_types:
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        signal_day = unix_nanos_to_dt(bar.ts_event).astimezone(SHANGHAI).date()
        instrument = self.canonical_by_native[str(bar.bar_type.instrument_id)]
        self._seen_by_day.setdefault(signal_day, set()).add(instrument)
        if (signal_day, instrument) not in self.suspended_keys:
            self._history[instrument].append((signal_day, bar.close.as_decimal()))
        if self._seen_by_day[signal_day] == set(self.native_by_canonical):
            self._compute_signal(signal_day)
            return
        if signal_day in self._signal_alert_days:
            return
        self._signal_alert_days.add(signal_day)
        self.clock.set_time_alert(
            f"momentum-signal-close-{signal_day.isoformat()}",
            datetime.combine(signal_day, time(15), tzinfo=SHANGHAI) + timedelta(microseconds=1),
            self._compute_signal_at_close,
            allow_past=False,
        )

    def _compute_signal_at_close(self, event) -> None:
        signal_day = unix_nanos_to_dt(event.ts_event).astimezone(SHANGHAI).date()
        self._compute_signal(signal_day)

    def _compute_signal(self, signal_day: date) -> None:
        if signal_day in self._processed_signal_days:
            return
        self._processed_signal_days.add(signal_day)
        lookback = self.spec.parameters["lookback_days"]
        candidates: list[tuple[Decimal, str]] = []
        for instrument, history in self._history.items():
            if len(history) <= lookback or history[-1][0] != signal_day:
                continue
            score = history[-1][1] / history[-lookback - 1][1] - Decimal(1)
            candidates.append((score, instrument))
        ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
        selected = ranked[: self.spec.parameters["top_k"]]
        if not selected:
            return
        weight = canonical_weight(len(selected))
        decisions = [
            RankedDecision(signal_day, instrument, weight, score) for score, instrument in selected
        ]
        self._decisions_by_day[signal_day] = decisions
        self.runtime_decisions.extend(decisions)
        self.decision_records.extend(item.as_dict() for item in decisions)
        next_day = next((day for day in self.trading_days if day > signal_day), None)
        if next_day is None:
            for decision in decisions:
                self._record_reject(decision, signal_day, "no_next_session", "BUY")
            return
        alert_name = f"momentum-next-open-{signal_day.isoformat()}"
        self._pending_by_alert[alert_name] = signal_day, next_day
        self.clock.set_time_alert(
            alert_name,
            datetime.combine(next_day, time(9, 30), tzinfo=SHANGHAI) + timedelta(microseconds=1),
            self._rebalance_at_next_open,
            allow_past=False,
        )

    def _rebalance_at_next_open(self, event) -> None:
        signal_day, execution_day = self._pending_by_alert.pop(event.name)
        decisions = self._decisions_by_day[signal_day]
        decisions_by_instrument = {item.instrument: item for item in decisions}
        equity = self.portfolio.equity(VENUE).get(CNY)
        if equity is None:
            raise RuntimeError("Nautilus portfolio has no CNY equity")
        investable_equity = equity.as_decimal() - self._estimated_sell_fees(decisions_by_instrument)
        target_quantity: dict[str, int] = {}
        for decision in decisions:
            price = self._open_price(decision.instrument)
            if price is None:
                self._record_reject(decision, execution_day, "no_bar", "BUY")
                continue
            gross_budget = investable_equity * Decimal(decision.target_weight)
            cash_budget = min(
                gross_budget - self.spec.fees.minimum_commission_cny,
                gross_budget / (Decimal(1) + self.spec.fees.commission_rate),
            )
            lots = (cash_budget / price / self.spec.lot_size).to_integral_value(
                rounding=ROUND_FLOOR
            )
            target_quantity[decision.instrument] = max(0, int(lots) * self.spec.lot_size)

        all_instruments = sorted(set(self.position_quantity) | set(target_quantity))
        submitted_sell = False
        sell_blocked = False
        for instrument in all_instruments:
            current = self.position_quantity.get(instrument, 0)
            target = target_quantity.get(instrument, 0)
            delta = target - current
            if delta >= 0:
                continue
            decision = decisions_by_instrument.get(
                instrument,
                RankedDecision(signal_day, instrument, "0", Decimal(0)),
            )
            rejection = self._execution_guard(decision, execution_day, OrderSide.SELL, abs(delta))
            if rejection is not None:
                self._record_reject(decision, execution_day, rejection, "SELL")
                sell_blocked = True
                continue
            self._submit_target_delta(decision, execution_day, OrderSide.SELL, abs(delta))
            submitted_sell = True
        buy_targets = {
            instrument: quantity
            for instrument, quantity in target_quantity.items()
            if quantity > self.position_quantity.get(instrument, 0)
        }
        if not buy_targets or sell_blocked:
            return
        if submitted_sell:
            alert_name = f"momentum-buy-after-sells-{signal_day.isoformat()}"
            self._pending_buys[alert_name] = (
                signal_day,
                execution_day,
                buy_targets,
                decisions_by_instrument,
            )
            self.clock.set_time_alert(
                alert_name,
                datetime.combine(execution_day, time(9, 30), tzinfo=SHANGHAI)
                + timedelta(microseconds=2),
                self._buy_after_sells,
                allow_past=False,
            )
        else:
            self._submit_buys(
                signal_day,
                execution_day,
                buy_targets,
                decisions_by_instrument,
            )

    def _buy_after_sells(self, event) -> None:
        self._submit_buys(*self._pending_buys.pop(event.name))

    def _submit_buys(
        self,
        signal_day: date,
        execution_day: date,
        targets: dict[str, int],
        decisions: dict[str, RankedDecision],
    ) -> None:
        for instrument in sorted(targets):
            quantity = targets[instrument] - self.position_quantity.get(instrument, 0)
            if quantity <= 0:
                continue
            decision = decisions.get(
                instrument,
                RankedDecision(signal_day, instrument, "0", Decimal(0)),
            )
            rejection = self._execution_guard(decision, execution_day, OrderSide.BUY, quantity)
            if rejection is not None:
                self._record_reject(decision, execution_day, rejection, "BUY")
                continue
            self._submit_target_delta(decision, execution_day, OrderSide.BUY, quantity)

    def _estimated_sell_fees(
        self,
        decisions: dict[str, RankedDecision],
    ) -> Decimal:
        total = Decimal(0)
        for instrument, quantity in self.position_quantity.items():
            if quantity <= 0 or instrument in decisions:
                continue
            price = self._open_price(instrument)
            if price is not None:
                total += calculate_fee(
                    Decimal(quantity) * price,
                    OrderSide.SELL,
                    self.spec.fees,
                )
        return total

    def _open_price(self, instrument: str) -> Decimal | None:
        quote = self.cache.quote_tick(self.native_by_canonical[instrument])
        if quote is None:
            return None
        return quote.ask_price.as_decimal()

    def _execution_guard(
        self,
        decision: RankedDecision,
        execution_day: date,
        side: OrderSide,
        quantity: int,
    ) -> str | None:
        state = self.rule_book.state_for(
            Decision(
                trading_day=execution_day,
                instrument=decision.instrument,
                signal="momentum_rebalance",
                target_quantity=quantity,
                order_intent=(
                    "buy_market_next_open" if side == OrderSide.BUY else "sell_market_next_open"
                ),
                expected_rule="allow",
            ),
            at_open=True,
        )
        if not state.has_bar:
            return "no_bar"
        if state.before_listing:
            return "pre_listing"
        if state.after_delisting:
            return "post_delisting"
        if state.suspended:
            return "suspended"
        if side == OrderSide.BUY and state.limit_up:
            return "limit_up"
        if side == OrderSide.SELL and state.limit_down:
            return "limit_down"
        if side == OrderSide.SELL:
            if self.position_quantity[decision.instrument] < quantity:
                return "insufficient_position"
            if self.acquired_day.get(decision.instrument) == execution_day:
                return "t_plus_one"
        return None

    def _submit_target_delta(
        self,
        decision: RankedDecision,
        execution_day: date,
        side: OrderSide,
        quantity: int,
    ) -> None:
        order = self.order_factory.market(
            instrument_id=self.native_by_canonical[decision.instrument],
            order_side=side,
            quantity=Quantity.from_int(quantity),
            time_in_force=TimeInForce.GTC,
        )
        self.order_records.append(
            {
                "instrument": decision.instrument,
                "quantity": quantity,
                "side": side.name,
                "signal_day": decision.signal_date.isoformat(),
                "submit_day": execution_day.isoformat(),
                "timing": "NEXT_OPEN",
                "type": "MARKET",
            }
        )
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        instrument = self.canonical_by_native[str(event.instrument_id)]
        fill_day = unix_nanos_to_dt(event.ts_event).astimezone(SHANGHAI).date()
        quantity = int(event.last_qty.as_decimal())
        if event.order_side == OrderSide.BUY:
            self.position_quantity[instrument] += quantity
            self.acquired_day[instrument] = fill_day
        else:
            self.position_quantity[instrument] -= quantity
            if self.position_quantity[instrument] == 0:
                self.acquired_day.pop(instrument, None)
        self.fill_records.append(
            {
                "fill_day": fill_day.isoformat(),
                "instrument": instrument,
                "price": str(event.last_px),
                "quantity": quantity,
                "side": event.order_side.name,
            }
        )
        self.fee_records.append(
            {
                "amount": str(event.commission.as_decimal()),
                "currency": str(event.commission.currency),
                "fill_day": fill_day.isoformat(),
                "instrument": instrument,
                "side": event.order_side.name,
            }
        )
        self.position_records.append(
            {
                "instrument": instrument,
                "quantity": self.position_quantity[instrument],
                "trading_day": fill_day.isoformat(),
            }
        )

    def on_stop(self) -> None:
        for signal_day in sorted(self._signal_alert_days - self._processed_signal_days):
            self._compute_signal(signal_day)

    def _record_reject(
        self,
        decision: RankedDecision,
        execution_day: date,
        reason: str,
        side: str,
    ) -> None:
        self.reject_records.append(
            {
                "execution_day": execution_day.isoformat(),
                "instrument": decision.instrument,
                "reason": reason,
                "side": side,
                "signal_day": decision.signal_date.isoformat(),
                "source": "nautilus_strategy_guard",
            }
        )
