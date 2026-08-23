from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Any

from quant_runtime.contracts.canonical_hash import canonical_json, normalize_decimal

from .catalog import CanonicalInstrument


@dataclass(frozen=True, slots=True)
class CanonicalBar:
    trading_day: date
    instrument: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    pre_close: Decimal
    is_suspended: bool
    is_st: bool

    @classmethod
    def from_markethub(
        cls,
        row: dict[str, Any],
        instrument_by_code: dict[str, CanonicalInstrument],
    ) -> CanonicalBar:
        code = str(row.get("code", ""))
        try:
            instrument = instrument_by_code[code]
        except KeyError as exc:
            raise ValueError(f"bar has unknown code {code!r}") from exc
        suspended = bool(row.get("is_suspended", False))
        pre_close = _decimal(row.get("pre_close"))
        fallback = pre_close if suspended else None
        result = cls(
            trading_day=date.fromisoformat(str(row["trade_time"])),
            instrument=instrument.instrument,
            open=_decimal(row.get("open"), fallback=fallback),
            high=_decimal(row.get("high"), fallback=fallback),
            low=_decimal(row.get("low"), fallback=fallback),
            close=_decimal(row.get("close"), fallback=fallback),
            volume=_decimal(row.get("volume"), fallback=Decimal(0) if suspended else None),
            amount=_decimal(row.get("amount"), fallback=Decimal(0) if suspended else None),
            pre_close=pre_close,
            is_suspended=suspended,
            is_st=bool(row.get("is_st", instrument.is_st)),
        )
        result.validate()
        return result

    @property
    def identity(self) -> tuple[date, str]:
        return self.trading_day, self.instrument

    def validate(self) -> None:
        values = (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.amount,
            self.pre_close,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError(f"non-finite daily row: {self.identity}")
        if self.volume < 0 or self.amount < 0 or self.pre_close <= 0:
            raise ValueError(f"invalid volume, amount, or pre_close: {self.identity}")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError(f"non-positive price: {self.identity}")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError(f"high below OHLC member: {self.identity}")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError(f"low above OHLC member: {self.identity}")

    def hash_record(self) -> dict[str, Any]:
        return {
            "amount": normalize_decimal(self.amount),
            "close": normalize_decimal(self.close),
            "high": normalize_decimal(self.high),
            "instrument": self.instrument,
            "is_st": self.is_st,
            "is_suspended": self.is_suspended,
            "low": normalize_decimal(self.low),
            "open": normalize_decimal(self.open),
            "pre_close": normalize_decimal(self.pre_close),
            "trading_day": self.trading_day.isoformat(),
            "volume": normalize_decimal(self.volume),
        }


@dataclass(frozen=True, slots=True)
class CanonicalDataset:
    data_version: str
    dataset_version: str
    timezone: str
    instruments: tuple[CanonicalInstrument, ...]
    trading_days: tuple[date, ...]
    bars: tuple[CanonicalBar, ...]

    def validate(self) -> None:
        if not self.data_version or not self.dataset_version:
            raise ValueError("canonical dataset requires both version identities")
        if self.timezone != "Asia/Shanghai":
            raise ValueError(f"unexpected timezone {self.timezone!r}")
        if not _strictly_increasing(item.instrument for item in self.instruments):
            raise ValueError("instruments are duplicated or out of canonical order")
        if not _strictly_increasing(iter(self.trading_days)):
            raise ValueError("trading days are duplicated or out of order")
        if not _strictly_increasing(item.identity for item in self.bars):
            raise ValueError("bars are duplicated or out of canonical order")
        instruments = {item.instrument for item in self.instruments}
        trading_days = set(self.trading_days)
        if any(item.instrument not in instruments for item in self.bars):
            raise ValueError("bar references an unknown instrument")
        if any(item.trading_day not in trading_days for item in self.bars):
            raise ValueError("bar falls outside the canonical trading calendar")

    @property
    def input_hash(self) -> str:
        self.validate()
        value = {
            "schema": "quant-runtime.canonical-daily.v1",
            "data_version": self.data_version,
            "dataset_version": self.dataset_version,
            "timezone": self.timezone,
            "instruments": [item.hash_record() for item in self.instruments],
            "trading_days": [item.isoformat() for item in self.trading_days],
            "bars": [item.hash_record() for item in self.bars],
        }
        return sha256(canonical_json(value)).hexdigest()


def _decimal(value: Any, *, fallback: Decimal | None = None) -> Decimal:
    if value is None:
        if fallback is None:
            raise ValueError("required daily numeric value is null")
        return fallback
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"non-finite daily numeric value {value!r}")
    return result


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
