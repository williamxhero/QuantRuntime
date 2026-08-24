from __future__ import annotations

from datetime import date
from decimal import Decimal

from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.data import Bar
from nautilus_trader.trading.strategy import Strategy

from quant_runtime.adapters.formal.nautilus.decisions import DecisionRecord


class ObservedBarFixtureStrategy(Strategy):
    """Test-only strategy proving decisions originate inside observed Nautilus bars."""

    def __init__(
        self,
        spec,
        fee_spec,
        lot_size: int,
        trading_days: tuple[date, ...],
        bar_types,
        canonical_by_native: dict[str, str],
        native_by_canonical,
        rule_book,
        suspended_keys,
    ) -> None:
        super().__init__()
        del fee_spec, lot_size, trading_days, native_by_canonical, rule_book, suspended_keys
        self.spec = spec
        self.bar_types = bar_types
        self.canonical_by_native = canonical_by_native
        self.runtime_decisions: list[DecisionRecord] = []
        self.decision_records: list[dict] = []
        self.order_records: list[dict] = []
        self.reject_records: list[dict] = []
        self.fill_records: list[dict] = []
        self.position_records: list[dict] = []
        self.fee_records: list[dict] = []

    def on_start(self) -> None:
        for bar_type in self.bar_types:
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        instrument = self.canonical_by_native[str(bar.bar_type.instrument_id)]
        signal_date = unix_nanos_to_dt(bar.ts_event).date()
        decision = DecisionRecord(signal_date, instrument, "0", Decimal(str(bar.close)))
        self.runtime_decisions.append(decision)
        self.decision_records.append(decision.as_dict())
