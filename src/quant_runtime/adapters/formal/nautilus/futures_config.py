from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any

from nautilus_trader.core.datetime import dt_to_unix_nanos

from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesDataset,
)

from .runner import StrategyContext


@dataclass(frozen=True, slots=True)
class FuturesCommissionSpec:
    per_contract: Decimal
    rate: Decimal

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FuturesCommissionSpec:
        if set(value) != {"per_contract", "rate"}:
            raise ValueError("futures commission action requires per_contract and rate")
        spec = cls(
            per_contract=Decimal(str(value["per_contract"])),
            rate=Decimal(str(value["rate"])),
        )
        if (
            not spec.per_contract.is_finite()
            or not spec.rate.is_finite()
            or min(spec.per_contract, spec.rate) < 0
        ):
            raise ValueError("futures commission values must be finite and non-negative")
        return spec


@dataclass(frozen=True, slots=True)
class FuturesContractSpec:
    instrument: str
    product_code: str
    exchange: str
    asset_class: str
    currency: str
    price_precision: int
    tick_size: Decimal
    multiplier: Decimal
    lot_size: Decimal
    margin_init: Decimal
    margin_maint: Decimal
    commission: dict[str, FuturesCommissionSpec]

    @classmethod
    def from_dict(cls, instrument: str, value: dict[str, Any]) -> FuturesContractSpec:
        required = {
            "product_code",
            "exchange",
            "asset_class",
            "currency",
            "price_precision",
            "tick_size",
            "multiplier",
            "lot_size",
            "margin_init",
            "margin_maint",
        }
        if missing := required - value.keys():
            raise ValueError(
                f"futures contract {instrument!r} lacks frozen fields: {sorted(missing)}"
            )
        commission = _commission_schedule(instrument, value)
        spec = cls(
            instrument=instrument,
            product_code=str(value["product_code"]),
            exchange=str(value["exchange"]),
            asset_class=str(value["asset_class"]),
            currency=str(value["currency"]),
            price_precision=int(value["price_precision"]),
            tick_size=Decimal(str(value["tick_size"])),
            multiplier=Decimal(str(value["multiplier"])),
            lot_size=Decimal(str(value["lot_size"])),
            margin_init=Decimal(str(value["margin_init"])),
            margin_maint=Decimal(str(value["margin_maint"])),
            commission=commission,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        decimals = (
            self.tick_size,
            self.multiplier,
            self.lot_size,
            self.margin_init,
            self.margin_maint,
            *(item.per_contract for item in self.commission.values()),
            *(item.rate for item in self.commission.values()),
        )
        if any(not value.is_finite() for value in decimals):
            raise ValueError(f"futures contract {self.instrument!r} contains non-finite values")
        if not self.product_code or not self.exchange or self.currency != "CNY":
            raise ValueError(f"futures contract {self.instrument!r} lacks CN market identity")
        if self.asset_class not in {"COMMODITY", "DEBT", "INDEX", "ALTERNATIVE"}:
            raise ValueError(f"futures contract {self.instrument!r} has invalid asset_class")
        if self.price_precision < 0 or min(self.tick_size, self.multiplier, self.lot_size) <= 0:
            raise ValueError(f"futures contract {self.instrument!r} has invalid native sizing")
        if not Decimal(0) < self.margin_init <= Decimal(1):
            raise ValueError(f"futures contract {self.instrument!r} has invalid initial margin")
        if not Decimal(0) < self.margin_maint <= self.margin_init:
            raise ValueError(f"futures contract {self.instrument!r} has invalid maintenance margin")

    @property
    def requires_commission_tag(self) -> bool:
        return len(set(self.commission.values())) > 1


@dataclass(frozen=True, slots=True)
class FuturesExecutionConfig:
    initial_cash_cny: Decimal
    slippage_ticks: Decimal
    contracts: dict[str, FuturesContractSpec]
    trading_days: tuple[date, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FuturesExecutionConfig:
        required = {"initial_cash_cny", "slippage_ticks", "contracts", "trading_days"}
        if missing := required - value.keys():
            raise ValueError(f"futures execution lacks frozen fields: {sorted(missing)}")
        raw_contracts = value["contracts"]
        if not isinstance(raw_contracts, dict) or not raw_contracts:
            raise ValueError("futures execution contracts must be a non-empty object")
        raw_trading_days = value["trading_days"]
        if not isinstance(raw_trading_days, list):
            raise ValueError("futures execution trading_days must be an array")
        config = cls(
            initial_cash_cny=Decimal(str(value["initial_cash_cny"])),
            slippage_ticks=Decimal(str(value["slippage_ticks"])),
            contracts={
                str(instrument): FuturesContractSpec.from_dict(str(instrument), spec)
                for instrument, spec in raw_contracts.items()
                if isinstance(spec, dict)
            },
            trading_days=tuple(date.fromisoformat(str(item)) for item in raw_trading_days),
        )
        if len(config.contracts) != len(raw_contracts):
            raise ValueError("each futures contract spec must be an object")
        config.validate()
        return config

    def validate(self) -> None:
        if not self.initial_cash_cny.is_finite() or self.initial_cash_cny <= 0:
            raise ValueError("futures initial_cash_cny must be positive")
        if not self.slippage_ticks.is_finite() or self.slippage_ticks < 0:
            raise ValueError("futures slippage_ticks must be finite and non-negative")
        if not self.trading_days or self.trading_days != tuple(sorted(set(self.trading_days))):
            raise ValueError("futures trading_days must be non-empty, unique, and ordered")

    def validate_dataset(self, dataset: CanonicalFuturesDataset) -> None:
        expected = {item.instrument for item in dataset.instruments}
        if set(self.contracts) != expected:
            raise ValueError(
                "frozen futures contract specs must exactly match the snapshot instruments"
            )
        for instrument in dataset.instruments:
            spec = self.contracts[instrument.instrument]
            if spec.product_code.casefold() != instrument.product_code.casefold():
                raise ValueError(f"futures product_code mismatch for {instrument.instrument!r}")
            if spec.exchange != instrument.exchange:
                raise ValueError(f"futures exchange mismatch for {instrument.instrument!r}")
        for bar in dataset.bars:
            _canonical_trading_day(bar, self.trading_days)


@dataclass(frozen=True, slots=True)
class FuturesSignalBar:
    bar: CanonicalFuturesBar
    trading_day: date

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bar, name)


@dataclass(frozen=True, slots=True)
class FuturesStrategyContext:
    strategy: StrategyContext
    dataset: CanonicalFuturesDataset
    instruments: dict[str, Any]
    contract_specs: dict[str, FuturesContractSpec]
    execution: FuturesExecutionConfig
    _signal_bars: dict[tuple[int, str], FuturesSignalBar] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_signal_bars",
            {
                (dt_to_unix_nanos(item.bar_time), item.instrument): FuturesSignalBar(
                    bar=item,
                    trading_day=_canonical_trading_day(
                        item,
                        self.execution.trading_days,
                    ),
                )
                for item in self.dataset.bars
            },
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return self.strategy.parameters

    def signal_bar(self, ts_event: int, instrument: str) -> FuturesSignalBar:
        """Return frozen back-adjusted signal OHLC plus its economic adjustment offset."""

        try:
            return self._signal_bars[(int(ts_event), instrument)]
        except KeyError as exc:
            raise KeyError(f"no frozen signal bar for {instrument!r} at {ts_event}") from exc


def _commission_schedule(
    instrument: str,
    value: dict[str, Any],
) -> dict[str, FuturesCommissionSpec]:
    raw = value.get("commission")
    actions = ("open", "close", "close_today")
    if raw is not None:
        if not isinstance(raw, dict) or set(raw) != set(actions):
            raise ValueError(
                f"futures contract {instrument!r} commission requires open/close/close_today"
            )
        if any(not isinstance(raw[action], dict) for action in actions):
            raise ValueError(f"futures contract {instrument!r} commission actions must be objects")
        return {action: FuturesCommissionSpec.from_dict(raw[action]) for action in actions}
    if "commission_per_contract" not in value or "commission_rate" not in value:
        raise ValueError(
            f"futures contract {instrument!r} requires commission schedule or uniform shorthand"
        )
    uniform = FuturesCommissionSpec.from_dict(
        {
            "per_contract": value["commission_per_contract"],
            "rate": value["commission_rate"],
        }
    )
    return {action: uniform for action in actions}


def _canonical_trading_day(
    bar: CanonicalFuturesBar,
    trading_days: tuple[date, ...],
) -> date:
    local_date = bar.bar_time.date()
    if bar.bar_time.time() < time(18) and local_date in trading_days:
        return local_date
    try:
        return next(item for item in trading_days if item > local_date)
    except StopIteration as exc:
        raise ValueError(
            f"frozen futures trading_days do not cover night session {bar.bar_time.isoformat()}"
        ) from exc
