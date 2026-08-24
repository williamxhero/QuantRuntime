from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Money

from quant_runtime.adapters.data.markethub.catalog import CanonicalInstrument
from quant_runtime.adapters.data.markethub.model import CanonicalBar, CanonicalDataset

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class FeeSpec:
    commission_rate: Decimal
    minimum_commission_cny: Decimal
    sell_stamp_duty_rate: Decimal
    currency_precision: int
    rounding_mode: str
    rounding_scope: str

    def validate(self) -> None:
        if self.currency_precision != 2:
            raise ValueError("formal fees require CNY cent precision")
        if self.rounding_mode != "half_away_from_zero" or self.rounding_scope != "per_fill":
            raise ValueError("formal fees require per-fill half-away-from-zero rounding")
        if (
            min(
                self.commission_rate,
                self.minimum_commission_cny,
                self.sell_stamp_duty_rate,
            )
            < 0
        ):
            raise ValueError("fee rates and minimum commission must be non-negative")


@dataclass(frozen=True, slots=True)
class RuleState:
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False
    has_bar: bool = True
    before_listing: bool = False
    after_delisting: bool = False


def calculate_fee(notional: Decimal, side: OrderSide, spec: FeeSpec) -> Decimal:
    commission = max(spec.minimum_commission_cny, notional * spec.commission_rate)
    if side == OrderSide.SELL:
        commission += notional * spec.sell_stamp_duty_rate
    return commission.quantize(CENT, rounding=ROUND_HALF_UP)


class AShareFeeModel(FeeModel):
    def __init__(self, spec: FeeSpec) -> None:
        super().__init__()
        self._spec = spec

    def get_commission(self, order, fill_qty, fill_px, instrument) -> Money:
        notional = fill_qty.as_decimal() * fill_px.as_decimal()
        return Money(calculate_fee(notional, order.side, self._spec), CNY)


class AShareRuleBook:
    def __init__(self, dataset: CanonicalDataset) -> None:
        self._instruments = {item.instrument: item for item in dataset.instruments}
        self._bars = {item.identity: item for item in dataset.bars}

    def state_for(
        self,
        trading_day: date,
        instrument: str,
        *,
        at_open: bool,
    ) -> RuleState:
        security = self._instruments[instrument]
        bar = self._bars.get((trading_day, instrument))
        return RuleState(
            has_bar=bar is not None,
            before_listing=security.list_date is not None and trading_day < security.list_date,
            after_delisting=(
                security.delist_date is not None and trading_day > security.delist_date
            ),
            suspended=bool(bar and bar.is_suspended),
            limit_up=bool(bar and _at_price_band(bar, security, upper=True, at_open=at_open)),
            limit_down=bool(bar and _at_price_band(bar, security, upper=False, at_open=at_open)),
        )


def _at_price_band(
    bar: CanonicalBar,
    instrument: CanonicalInstrument,
    *,
    upper: bool,
    at_open: bool,
) -> bool:
    rate = _price_limit_rate(instrument, bar)
    factor = Decimal(1) + rate if upper else Decimal(1) - rate
    limit = (bar.pre_close * factor).quantize(instrument.tick_size, rounding=ROUND_HALF_UP)
    observed = bar.open if at_open else bar.close
    return observed >= limit if upper else observed <= limit


def _price_limit_rate(instrument: CanonicalInstrument, bar: CanonicalBar) -> Decimal:
    if instrument.is_st or bar.is_st:
        return Decimal("0.05")
    if instrument.exchange == "BJSE":
        return Decimal("0.30")
    if instrument.raw_code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    return Decimal("0.10")
