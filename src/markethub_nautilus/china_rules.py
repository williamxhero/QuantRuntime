from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Money

from .canonical import CanonicalBar, CanonicalDataset, CanonicalInstrument
from .config import Decision, FeeSpec, RuleState

CENT = Decimal("0.01")


def calculate_fee(notional: Decimal, side: OrderSide, spec: FeeSpec) -> Decimal:
    commission = max(spec.minimum_commission_cny, notional * spec.commission_rate)
    if side == OrderSide.SELL:
        commission += notional * spec.sell_stamp_duty_rate
    return commission.quantize(CENT, rounding=ROUND_HALF_UP)


class AShareFeeModel(FeeModel):
    """Minimum commission plus sell stamp duty, rounded for every fill."""

    def __init__(self, spec: FeeSpec) -> None:
        super().__init__()
        self._spec = spec

    def get_commission(self, order, fill_qty, fill_px, instrument) -> Money:
        notional = fill_qty.as_decimal() * fill_px.as_decimal()
        return Money(calculate_fee(notional, order.side, self._spec), CNY)


class AShareRuleBook:
    def __init__(
        self,
        dataset: CanonicalDataset,
        overrides: dict[tuple[date, str], RuleState] | None = None,
    ) -> None:
        self._instruments = {item.instrument: item for item in dataset.instruments}
        self._bars = {item.identity: item for item in dataset.bars}
        self._overrides = overrides or {}

    def state_for(self, decision: Decision) -> RuleState:
        override = self._overrides.get((decision.trading_day, decision.instrument))
        if override is not None:
            return override
        instrument = self._instruments[decision.instrument]
        bar = self._bars.get((decision.trading_day, decision.instrument))
        return RuleState(
            has_bar=bar is not None,
            before_listing=(
                instrument.list_date is not None and decision.trading_day < instrument.list_date
            ),
            after_delisting=(
                instrument.delist_date is not None and decision.trading_day > instrument.delist_date
            ),
            suspended=bool(bar and bar.is_suspended),
            limit_up=bool(bar and _at_price_band(bar, instrument, upper=True)),
            limit_down=bool(bar and _at_price_band(bar, instrument, upper=False)),
        )


def _at_price_band(
    bar: CanonicalBar,
    instrument: CanonicalInstrument,
    *,
    upper: bool,
) -> bool:
    rate = _price_limit_rate(instrument, bar)
    factor = Decimal(1) + rate if upper else Decimal(1) - rate
    limit = (bar.pre_close * factor).quantize(instrument.tick_size, rounding=ROUND_HALF_UP)
    observed = bar.close
    return observed >= limit if upper else observed <= limit


def _price_limit_rate(instrument: CanonicalInstrument, bar: CanonicalBar) -> Decimal:
    if instrument.is_st or bar.is_st:
        return Decimal("0.05")
    if instrument.exchange == "BJSE":
        return Decimal("0.30")
    if instrument.raw_code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    return Decimal("0.10")
