from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from quant_runtime.adapters.formal.nautilus import FormalDecisionRecord


class NativeFuturesFixtureStrategy(Strategy):
    def __init__(
        self,
        *,
        context,
        bar_types,
        canonical_by_native,
        native_by_canonical,
    ) -> None:
        super().__init__()
        self.context = context
        self.bar_types = bar_types
        self.canonical_by_native = canonical_by_native
        self.native_by_canonical = native_by_canonical
        self.runtime_decisions: list[FormalDecisionRecord] = []
        self.order_records: list[dict] = []
        self.reject_records: list[dict] = []
        self.fill_records: list[dict] = []
        self.position_records: list[dict] = []
        self.fee_records: list[dict] = []
        self._submitted = False

    def on_start(self) -> None:
        for bar_type in self.bar_types:
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        canonical = self.canonical_by_native[str(bar.bar_type.instrument_id)]
        signal = self.context.signal_bar(bar.ts_event, canonical)
        assert signal.signal_close + signal.adjustment_offset == signal.economic_close
        if self._submitted:
            return
        self._submitted = True
        self.runtime_decisions.append(
            FormalDecisionRecord(
                ts_event=bar.ts_event,
                instrument=canonical,
                intent="order",
                payload={"order_type": "MARKET", "quantity": "1", "side": "BUY"},
            )
        )
        order = self.order_factory.market(
            instrument_id=self.native_by_canonical[canonical],
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            tags=["commission:open"],
        )
        self.submit_order(order)
