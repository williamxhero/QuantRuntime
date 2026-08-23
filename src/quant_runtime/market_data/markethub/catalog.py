from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from quant_runtime.contracts.canonical_hash import normalize_decimal

EXCHANGE_TO_PREFIX = {
    "SHSE": "SH",
    "SSE": "SH",
    "SZSE": "SZ",
    "BSE": "BJ",
    "BJSE": "BJ",
}
PREFIX_TO_EXCHANGE = {"SH": "SHSE", "SZ": "SZSE", "BJ": "BJSE"}


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
        code = str(row.get("code", ""))
        exchange = str(row.get("exchange", "")).upper()
        try:
            prefix = EXCHANGE_TO_PREFIX[exchange]
        except KeyError as exc:
            raise ValueError(f"unsupported exchange {exchange!r}") from exc
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"invalid A-share code {code!r}")
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


def _optional_date(value: Any) -> date | None:
    if value is None or not str(value).strip():
        return None
    return date.fromisoformat(str(value))
