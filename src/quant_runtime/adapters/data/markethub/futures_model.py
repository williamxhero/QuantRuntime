from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from quant_runtime.artifacts import canonical_json, normalize_decimal


@dataclass(frozen=True, slots=True)
class CanonicalFuturesInstrument:
    instrument: str
    product_code: str
    exchange: str
    series_type: str

    def hash_record(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "instrument": self.instrument,
            "product_code": self.product_code,
            "series_type": self.series_type,
        }


@dataclass(frozen=True, slots=True)
class CanonicalFuturesBar:
    bar_time: datetime
    instrument: str
    signal_open: Decimal
    signal_high: Decimal
    signal_low: Decimal
    signal_close: Decimal
    volume: Decimal
    open_interest: Decimal | None
    adjustment_offset: Decimal

    @classmethod
    def from_markethub(
        cls,
        row: dict[str, Any],
        instrument_by_product: dict[str, CanonicalFuturesInstrument],
        *,
        parse_time,
    ) -> CanonicalFuturesBar:
        product_code = str(row.get("product_code", "")).casefold()
        try:
            instrument = instrument_by_product[product_code]
        except KeyError as exc:
            raise ValueError(f"futures bar has unknown product_code {product_code!r}") from exc
        if str(row.get("exchange", "")) != instrument.exchange:
            raise ValueError(f"futures exchange drifted for {instrument.instrument!r}")
        if str(row.get("series_type", "")) != instrument.series_type:
            raise ValueError(f"futures series_type drifted for {instrument.instrument!r}")
        result = cls(
            bar_time=parse_time(row.get("bar_time")),
            instrument=instrument.instrument,
            signal_open=_decimal(row.get("open")),
            signal_high=_decimal(row.get("high")),
            signal_low=_decimal(row.get("low")),
            signal_close=_decimal(row.get("close")),
            volume=_decimal(row.get("volume")),
            open_interest=_optional_decimal(row.get("open_interest")),
            adjustment_offset=_decimal(row.get("adjustment_offset")),
        )
        result.validate()
        return result

    @property
    def identity(self) -> tuple[datetime, str]:
        return self.bar_time, self.instrument

    @property
    def economic_open(self) -> Decimal:
        return self.signal_open + self.adjustment_offset

    @property
    def economic_high(self) -> Decimal:
        return self.signal_high + self.adjustment_offset

    @property
    def economic_low(self) -> Decimal:
        return self.signal_low + self.adjustment_offset

    @property
    def economic_close(self) -> Decimal:
        return self.signal_close + self.adjustment_offset

    def validate(self) -> None:
        values = (
            self.signal_open,
            self.signal_high,
            self.signal_low,
            self.signal_close,
            self.volume,
            self.adjustment_offset,
            self.economic_open,
            self.economic_high,
            self.economic_low,
            self.economic_close,
        )
        if self.bar_time.tzinfo is None or any(not value.is_finite() for value in values):
            raise ValueError(f"invalid futures 1m row: {self.identity}")
        if self.open_interest is not None and (
            not self.open_interest.is_finite() or self.open_interest < 0
        ):
            raise ValueError(f"invalid futures open interest: {self.identity}")
        if self.volume < 0:
            raise ValueError(f"negative futures volume or open interest: {self.identity}")
        if min(self.signal_open, self.signal_high, self.signal_low, self.signal_close) <= 0:
            raise ValueError(f"non-positive signal price: {self.identity}")
        if min(self.economic_open, self.economic_high, self.economic_low, self.economic_close) <= 0:
            raise ValueError(f"non-positive economic price: {self.identity}")
        if self.signal_high < max(self.signal_open, self.signal_low, self.signal_close):
            raise ValueError(f"signal high below OHLC member: {self.identity}")
        if self.signal_low > min(self.signal_open, self.signal_high, self.signal_close):
            raise ValueError(f"signal low above OHLC member: {self.identity}")

    def hash_record(self) -> dict[str, Any]:
        return {
            "adjustment_offset": normalize_decimal(self.adjustment_offset),
            "bar_time": self.bar_time.isoformat(),
            "instrument": self.instrument,
            "open_interest": (
                normalize_decimal(self.open_interest) if self.open_interest is not None else None
            ),
            "signal_close": normalize_decimal(self.signal_close),
            "signal_high": normalize_decimal(self.signal_high),
            "signal_low": normalize_decimal(self.signal_low),
            "signal_open": normalize_decimal(self.signal_open),
            "volume": normalize_decimal(self.volume),
        }


@dataclass(frozen=True, slots=True)
class CanonicalFuturesDataset:
    data_version: str
    dataset_version: str
    timezone: str
    series_type: str
    instruments: tuple[CanonicalFuturesInstrument, ...]
    bars: tuple[CanonicalFuturesBar, ...]

    def validate(self) -> None:
        if not self.data_version or not self.dataset_version:
            raise ValueError("canonical futures dataset requires both version identities")
        if self.timezone != "Asia/Shanghai" or self.series_type not in {
            "back_adjusted_continuous",
            "main_continuous",
        }:
            raise ValueError("invalid futures timezone or series type")
        if not _strictly_increasing(item.instrument for item in self.instruments):
            raise ValueError("futures instruments are duplicated or out of canonical order")
        if not _strictly_increasing(item.identity for item in self.bars):
            raise ValueError("futures bars are duplicated or out of canonical order")
        instruments = {item.instrument for item in self.instruments}
        if any(item.instrument not in instruments for item in self.bars):
            raise ValueError("futures bar references an unknown instrument")
        if {item.instrument for item in self.bars} != instruments:
            raise ValueError("futures dataset has no bars for one or more instruments")

    @property
    def input_hash(self) -> str:
        self.validate()
        value = {
            "schema": "quant-runtime.canonical-futures-1m.v1",
            "data_version": self.data_version,
            "dataset_version": self.dataset_version,
            "timezone": self.timezone,
            "series_type": self.series_type,
            "instruments": [item.hash_record() for item in self.instruments],
            "bars": [item.hash_record() for item in self.bars],
        }
        return sha256(canonical_json(value)).hexdigest()


def product_code_from_instrument(instrument: str) -> str:
    value = instrument.removesuffix("L0")
    if not value or value == instrument:
        raise ValueError(
            f"continuous futures instrument must use the MarketHub <product>L0 form: {instrument!r}"
        )
    return value


def _decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("required futures numeric value is null")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"non-finite futures numeric value {value!r}")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _strictly_increasing(values: Iterable[Any]) -> bool:
    iterator = iter(values)
    try:
        previous = next(iterator)
    except StopIteration:
        return True
    for current in iterator:
        if current <= previous:
            return False
        previous = current
    return True
