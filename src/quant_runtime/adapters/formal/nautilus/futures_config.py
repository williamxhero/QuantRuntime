from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from nautilus_trader.core.datetime import dt_to_unix_nanos

from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesDataset,
    FuturesContractCatalogIdentity,
)
from quant_runtime.artifacts import sha256_value

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
class FuturesCoverageSpec:
    rows: int
    first_bar_time: datetime
    last_bar_time: datetime

    @classmethod
    def from_dict(cls, instrument: str, value: dict[str, Any]) -> FuturesCoverageSpec:
        if set(value) != {"rows", "first_bar_time", "last_bar_time"}:
            raise ValueError(
                f"futures coverage {instrument!r} requires rows/first_bar_time/last_bar_time"
            )
        result = cls(
            rows=int(value["rows"]),
            first_bar_time=_futures_datetime(value["first_bar_time"]),
            last_bar_time=_futures_datetime(value["last_bar_time"]),
        )
        if result.rows <= 0 or result.first_bar_time > result.last_bar_time:
            raise ValueError(f"futures coverage {instrument!r} is invalid")
        return result


@dataclass(frozen=True, slots=True)
class FuturesExecutionProfile:
    contract_catalog: FuturesContractCatalogIdentity
    commission_margin_source_id: str
    commission_margin_source_sha256: str
    commission_margin_effective_at: str
    commission_margin_decoder: str
    historical_rate_policy: str
    margin_maint_policy: str
    lot_size_policy: str
    close_priority: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FuturesExecutionProfile:
        required = {
            "schema",
            "contract_catalog",
            "commission_margin",
            "historical_rate_policy",
            "margin_maint_policy",
            "lot_size_policy",
            "close_priority",
        }
        if set(value) != required:
            raise ValueError(
                "futures execution profile requires exactly the frozen v1 evidence fields"
            )
        if value["schema"] != "quant-runtime.cn-futures-execution-profile.v1":
            raise ValueError("unsupported futures execution profile schema")
        catalog = value["contract_catalog"]
        source = value["commission_margin"]
        if not isinstance(catalog, dict) or set(catalog) != {
            "schema_version",
            "dataset_version",
            "snapshot_id",
            "content_checksum",
        }:
            raise ValueError("futures execution profile contract_catalog is invalid")
        if not isinstance(source, dict) or set(source) != {
            "source_id",
            "source_sha256",
            "effective_at",
            "decoder",
        }:
            raise ValueError("futures execution profile commission_margin is invalid")
        result = cls(
            contract_catalog=FuturesContractCatalogIdentity(
                schema_version=str(catalog["schema_version"]),
                dataset_version=str(catalog["dataset_version"]),
                snapshot_id=str(catalog["snapshot_id"]),
                content_checksum=str(catalog["content_checksum"]),
            ),
            commission_margin_source_id=str(source["source_id"]),
            commission_margin_source_sha256=str(source["source_sha256"]),
            commission_margin_effective_at=str(source["effective_at"]),
            commission_margin_decoder=str(source["decoder"]),
            historical_rate_policy=str(value["historical_rate_policy"]),
            margin_maint_policy=str(value["margin_maint_policy"]),
            lot_size_policy=str(value["lot_size_policy"]),
            close_priority=str(value["close_priority"]),
        )
        if not all(
            (
                result.contract_catalog.schema_version,
                result.contract_catalog.dataset_version,
                result.contract_catalog.snapshot_id,
                result.contract_catalog.content_checksum,
                result.commission_margin_source_id,
                result.commission_margin_effective_at,
                result.commission_margin_decoder,
                result.historical_rate_policy,
                result.margin_maint_policy,
                result.lot_size_policy,
                result.close_priority,
            )
        ):
            raise ValueError("futures execution profile contains empty evidence fields")
        if len(result.commission_margin_source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in result.commission_margin_source_sha256
        ):
            raise ValueError("futures execution profile source_sha256 must be lowercase sha256")
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "quant-runtime.cn-futures-execution-profile.v1",
            "contract_catalog": self.contract_catalog.hash_record(),
            "commission_margin": {
                "source_id": self.commission_margin_source_id,
                "source_sha256": self.commission_margin_source_sha256,
                "effective_at": self.commission_margin_effective_at,
                "decoder": self.commission_margin_decoder,
            },
            "historical_rate_policy": self.historical_rate_policy,
            "margin_maint_policy": self.margin_maint_policy,
            "lot_size_policy": self.lot_size_policy,
            "close_priority": self.close_priority,
        }


@dataclass(frozen=True, slots=True)
class FuturesExecutionConfig:
    initial_cash_cny: Decimal
    slippage_ticks: Decimal
    contracts: dict[str, FuturesContractSpec]
    trading_days: tuple[date, ...]
    coverage: dict[str, FuturesCoverageSpec] | None = None
    profile: FuturesExecutionProfile | None = None

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
            coverage=_coverage_schedule(value.get("coverage")),
            profile=(
                FuturesExecutionProfile.from_dict(value["profile"])
                if isinstance(value.get("profile"), dict)
                else None
            ),
        )
        if len(config.contracts) != len(raw_contracts):
            raise ValueError("each futures contract spec must be an object")
        if value.get("profile") is not None and config.profile is None:
            raise ValueError("futures execution profile must be an object")
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
            if instrument.tick_size is not None and (
                spec.tick_size != instrument.tick_size
                or spec.multiplier != instrument.multiplier
                or spec.price_precision != instrument.price_precision
                or spec.currency != instrument.currency
            ):
                raise ValueError(
                    f"frozen native contract spec mismatch for {instrument.instrument!r}"
                )
        if dataset.contract_catalog is not None:
            if self.profile is None or self.coverage is None:
                raise ValueError(
                    "catalog-bound futures snapshots require frozen profile and coverage"
                )
            if self.profile.contract_catalog != dataset.contract_catalog:
                raise ValueError("frozen execution profile contract catalog identity mismatch")
        if self.coverage is not None:
            if set(self.coverage) != expected:
                raise ValueError("frozen futures coverage must exactly match snapshot instruments")
            counts = dataset.bar_counts
            bounds = dataset.instrument_bounds
            for instrument, frozen in self.coverage.items():
                if counts[instrument] != frozen.rows or bounds[instrument] != (
                    frozen.first_bar_time,
                    frozen.last_bar_time,
                ):
                    raise ValueError(
                        f"frozen futures coverage mismatch for {instrument!r}: "
                        f"expected {frozen!r}, got rows={counts[instrument]!r}, "
                        f"bounds={bounds[instrument]!r}"
                    )
        # A partial publication is a replayable stream.  Its complete canonical
        # scan is frozen at snapshot resolution and every streamed bar is checked
        # in FuturesStrategyContext before Nautilus observes it.
        if not hasattr(dataset.bars, "verification"):
            for bar in dataset.bars:
                _canonical_trading_day(bar, self.trading_days)

    @property
    def profile_hash(self) -> str | None:
        return sha256_value(self.profile.as_dict()) if self.profile is not None else None


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
    streaming: bool = False
    _signal_bars: dict[tuple[int, str], FuturesSignalBar] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_signal_bars",
            {},
        )
        if not self.streaming:
            self.load_signal_batch(self.dataset.bars)

    @property
    def parameters(self) -> dict[str, Any]:
        return self.strategy.parameters

    def signal_bar(self, ts_event: int, instrument: str) -> FuturesSignalBar:
        """Return frozen back-adjusted signal OHLC plus its economic adjustment offset."""

        try:
            return self._signal_bars[(int(ts_event), instrument)]
        except KeyError as exc:
            raise KeyError(f"no frozen signal bar for {instrument!r} at {ts_event}") from exc

    def load_signal_batch(self, bars: Iterable[CanonicalFuturesBar]) -> None:
        """Replace the ephemeral signal sidecar used by one native streaming batch."""

        self._signal_bars.clear()
        self._signal_bars.update(
            {
                (dt_to_unix_nanos(item.bar_time), item.instrument): FuturesSignalBar(
                    bar=item,
                    trading_day=_canonical_trading_day(item, self.execution.trading_days),
                )
                for item in bars
            }
        )


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


def _coverage_schedule(value: Any) -> dict[str, FuturesCoverageSpec] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not value
        or any(not isinstance(item, dict) for item in value.values())
    ):
        raise ValueError("futures execution coverage must be a non-empty object")
    return {
        str(instrument): FuturesCoverageSpec.from_dict(str(instrument), item)
        for instrument, item in value.items()
    }


def _futures_datetime(value: Any) -> datetime:
    rendered = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(rendered)
    shanghai = ZoneInfo("Asia/Shanghai")
    return parsed.replace(tzinfo=shanghai) if parsed.tzinfo is None else parsed.astimezone(shanghai)
