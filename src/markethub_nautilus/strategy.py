from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from .china_rules import AShareRuleBook
from .config import Decision, StrategySpec

SHANGHAI = ZoneInfo("Asia/Shanghai")


class DecisionStrategy(Strategy):
    """Replays canonical next-open decisions while Nautilus owns execution and accounting."""

    def __init__(
        self,
        spec: StrategySpec,
        bar_types: tuple[BarType, ...],
        trading_days: tuple[date, ...],
        canonical_by_native: dict[str, str],
        native_by_canonical: dict[str, object],
        rule_book: AShareRuleBook,
    ) -> None:
        super().__init__()
        self.spec = spec
        self.bar_types = bar_types
        self.trading_days = trading_days
        self.canonical_by_native = canonical_by_native
        self.native_by_canonical = native_by_canonical
        self.rule_book = rule_book
        self.decisions_by_key = {
            (item.trading_day, item.instrument): item for item in spec.decisions
        }
        self.position_quantity = {instrument: 0 for instrument in native_by_canonical}
        self.acquired_day: dict[str, date] = {}
        self.decision_records: list[dict[str, object]] = []
        self.order_records: list[dict[str, object]] = []
        self.reject_records: list[dict[str, object]] = []
        self.fill_records: list[dict[str, object]] = []
        self.position_records: list[dict[str, object]] = []
        self.fee_records: list[dict[str, object]] = []
        self._pending_by_alert: dict[str, Decision] = {}

    def on_start(self) -> None:
        for bar_type in self.bar_types:
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        day = unix_nanos_to_dt(bar.ts_event).astimezone(SHANGHAI).date()
        canonical = self.canonical_by_native[str(bar.bar_type.instrument_id)]
        decision = self.decisions_by_key.get((day, canonical))
        if decision is None:
            return
        self.decision_records.append(decision.as_dict())
        rejection = self._guard(decision)
        if rejection is not None:
            self._record_reject(decision, rejection)
            return
        next_day = next((item for item in self.trading_days if item > day), None)
        if next_day is None:
            self._record_reject(decision, "no_next_session")
            return
        alert_name = f"next-open-{len(self._pending_by_alert) + len(self.order_records) + 1}"
        self._pending_by_alert[alert_name] = decision
        self.clock.set_time_alert(
            alert_name,
            datetime.combine(next_day, time(9, 30), tzinfo=SHANGHAI) + timedelta(microseconds=1),
            self._submit_at_next_open,
            allow_past=False,
        )

    def _submit_at_next_open(self, event) -> None:
        decision = self._pending_by_alert.pop(event.name)
        side = OrderSide.BUY if decision.order_intent.startswith("buy") else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=self.native_by_canonical[decision.instrument],
            order_side=side,
            quantity=Quantity.from_int(self.spec.lot_size),
            time_in_force=TimeInForce.GTC,
        )
        submit_day = unix_nanos_to_dt(event.ts_event).astimezone(SHANGHAI).date()
        self.order_records.append(
            {
                "instrument": decision.instrument,
                "quantity": self.spec.lot_size,
                "side": side.name,
                "signal_day": decision.trading_day.isoformat(),
                "submit_day": submit_day.isoformat(),
                "timing": "NEXT_OPEN",
                "type": "MARKET",
            }
        )
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        canonical = self.canonical_by_native[str(event.instrument_id)]
        fill_day = unix_nanos_to_dt(event.ts_event).astimezone(SHANGHAI).date()
        quantity = int(event.last_qty.as_decimal())
        if event.order_side == OrderSide.BUY:
            self.position_quantity[canonical] += quantity
            self.acquired_day[canonical] = fill_day
        else:
            self.position_quantity[canonical] -= quantity
            if self.position_quantity[canonical] == 0:
                self.acquired_day.pop(canonical, None)
        self.fill_records.append(
            {
                "fill_day": fill_day.isoformat(),
                "instrument": canonical,
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
                "instrument": canonical,
                "side": event.order_side.name,
            }
        )
        self.position_records.append(
            {
                "instrument": canonical,
                "quantity": self.position_quantity[canonical],
                "trading_day": fill_day.isoformat(),
            }
        )

    def on_stop(self) -> None:
        seen = {
            (date.fromisoformat(str(row["trading_day"])), str(row["instrument"]))
            for row in self.decision_records
        }
        for decision in self.spec.decisions:
            if (decision.trading_day, decision.instrument) not in seen:
                self.decision_records.append(decision.as_dict())
                self._record_reject(decision, "no_bar")
        self.decision_records.sort(
            key=lambda row: (str(row["trading_day"]), str(row["instrument"]))
        )
        self.reject_records.sort(key=lambda row: (str(row["trading_day"]), str(row["instrument"])))

    def _record_reject(self, decision: Decision, reason: str) -> None:
        self.reject_records.append(
            {
                "instrument": decision.instrument,
                "quantity": self.spec.lot_size,
                "reason": reason,
                "side": _side_text(decision),
                "source": "nautilus_strategy_guard",
                "trading_day": decision.trading_day.isoformat(),
            }
        )

    def _guard(self, decision: Decision) -> str | None:
        state = self.rule_book.state_for(decision)
        side = _side_text(decision)
        if not state.has_bar:
            return "no_bar"
        if state.before_listing:
            return "pre_listing"
        if state.after_delisting:
            return "post_delisting"
        if state.suspended:
            return "suspended"
        if side == "BUY" and state.limit_up:
            return "limit_up"
        if side == "SELL" and state.limit_down:
            return "limit_down"
        if side == "SELL":
            if self.position_quantity[decision.instrument] < self.spec.lot_size:
                return "insufficient_position"
            if self.acquired_day.get(decision.instrument) == decision.trading_day:
                return "t_plus_one"
        return None


def _side_text(decision: Decision) -> str:
    return "BUY" if decision.order_intent.startswith("buy") else "SELL"
