from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

EXCHANGE_TO_PREFIX = {
    "SHSE": "SH",
    "SSE": "SH",
    "SZSE": "SZ",
    "BSE": "BJ",
    "BJSE": "BJ",
}
PREFIX_TO_EXCHANGE = {"SH": "SHSE", "SZ": "SZSE", "BJ": "BJSE"}


class CanonicalDataError(ValueError):
    """MarketHub data did not satisfy the canonical daily contract."""


def normalize_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalDataError(f"non-finite decimal: {value}")
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalInstrument:
    instrument: str
    raw_code: str
    exchange: str
    currency: str
    price_precision: int
    tick_size: Decimal
    lot_size: int
    list_date: date | None
    delist_date: date | None
    is_st: bool = False

    @classmethod
    def from_catalog(cls, row: dict[str, Any]) -> CanonicalInstrument:
        code = str(row["code"])
        exchange = str(row["exchange"]).upper()
        try:
            prefix = EXCHANGE_TO_PREFIX[exchange]
        except KeyError as exc:
            raise CanonicalDataError(f"unsupported exchange {exchange!r}") from exc
        if len(code) != 6 or not code.isdigit():
            raise CanonicalDataError(f"invalid A-share code {code!r}")
        return cls(
            instrument=f"{prefix}.{code}",
            raw_code=code,
            exchange=PREFIX_TO_EXCHANGE[prefix],
            currency="CNY",
            price_precision=2,
            tick_size=Decimal("0.01"),
            lot_size=100,
            list_date=_optional_date(row.get("list_date")),
            delist_date=_optional_date(row.get("delist_date")),
            is_st=bool(row.get("is_st", False)),
        )

    def hash_record(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "delist_date": self.delist_date.isoformat() if self.delist_date else None,
            "exchange": self.exchange,
            "instrument": self.instrument,
            "is_st": self.is_st,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "lot_size": self.lot_size,
            "price_precision": self.price_precision,
            "raw_code": self.raw_code,
            "tick_size": normalize_decimal(self.tick_size),
        }


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
        code = str(row["code"])
        try:
            instrument = instrument_by_code[code]
        except KeyError as exc:
            raise CanonicalDataError(f"bar has unknown code {code}") from exc
        result = cls(
            trading_day=date.fromisoformat(str(row["trade_time"])),
            instrument=instrument.instrument,
            open=_decimal(row["open"]),
            high=_decimal(row["high"]),
            low=_decimal(row["low"]),
            close=_decimal(row["close"]),
            volume=_decimal(row["volume"]),
            amount=_decimal(row["amount"]),
            pre_close=_decimal(row["pre_close"]),
            is_suspended=bool(row.get("is_suspended", False)),
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
            raise CanonicalDataError(f"non-finite OHLCV row: {self.identity}")
        if self.volume < 0 or self.amount < 0:
            raise CanonicalDataError(f"negative volume/amount: {self.identity}")
        if self.pre_close <= 0:
            raise CanonicalDataError(f"non-positive pre_close: {self.identity}")
        if self.is_suspended and self.volume == 0:
            return
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise CanonicalDataError(f"non-positive price: {self.identity}")
        if self.high < max(self.open, self.low, self.close):
            raise CanonicalDataError(f"high below OHLC member: {self.identity}")
        if self.low > min(self.open, self.high, self.close):
            raise CanonicalDataError(f"low above OHLC member: {self.identity}")

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
    timezone: str
    instruments: tuple[CanonicalInstrument, ...]
    trading_days: tuple[date, ...]
    bars: tuple[CanonicalBar, ...]

    def validate(self) -> None:
        if not self.data_version:
            raise CanonicalDataError("empty data_version")
        if self.timezone != "Asia/Shanghai":
            raise CanonicalDataError(f"unexpected timezone {self.timezone}")
        if not _strictly_increasing(item.instrument for item in self.instruments):
            raise CanonicalDataError("instruments are duplicated or out of canonical order")
        if not _strictly_increasing(iter(self.trading_days)):
            raise CanonicalDataError("trading days are duplicated or out of order")
        if not _strictly_increasing(bar.identity for bar in self.bars):
            raise CanonicalDataError("bars are duplicated or out of canonical order")
        instruments = {item.instrument for item in self.instruments}
        days = set(self.trading_days)
        if any(bar.instrument not in instruments for bar in self.bars):
            raise CanonicalDataError("bar references an unknown instrument")
        if any(bar.trading_day not in days for bar in self.bars):
            raise CanonicalDataError("bar falls outside the canonical trading calendar")

    @property
    def input_hash(self) -> str:
        self.validate()
        digest = hashlib.sha256()
        digest.update(b'{"bars":[')
        _update_array(digest, (bar.hash_record() for bar in self.bars))
        digest.update(b'],"data_version":')
        digest.update(canonical_json(self.data_version))
        digest.update(b',"instruments":[')
        _update_array(digest, (item.hash_record() for item in self.instruments))
        digest.update(b'],"schema":"markethub-canonical-daily-v1","timezone":')
        digest.update(canonical_json(self.timezone))
        digest.update(b',"trading_days":[')
        _update_array(digest, (item.isoformat() for item in self.trading_days))
        digest.update(b"]}")
        return digest.hexdigest()


def _decimal(value: Any) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise CanonicalDataError(f"non-finite decimal {value!r}")
    return result


def _optional_date(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    return date.fromisoformat(str(value))


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


def _update_array(digest: Any, values: Iterable[Any]) -> None:
    first = True
    for value in values:
        if not first:
            digest.update(b",")
        digest.update(canonical_json(value))
        first = False
