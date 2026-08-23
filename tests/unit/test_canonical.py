from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from markethub_nautilus.canonical import (
    CanonicalBar,
    CanonicalDataError,
    CanonicalDataset,
    CanonicalInstrument,
)


def _instrument(code: str = "600000", exchange: str = "SHSE") -> CanonicalInstrument:
    return CanonicalInstrument.from_catalog(
        {"code": code, "exchange": exchange, "list_date": "2000-01-01"}
    )


def _bar(instrument: CanonicalInstrument, close: str = "10.5") -> CanonicalBar:
    return CanonicalBar(
        trading_day=date(2025, 1, 2),
        instrument=instrument.instrument,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9.9"),
        close=Decimal(close),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
        pre_close=Decimal("10.1"),
        is_suspended=False,
        is_st=False,
    )


def test_sh_sz_bj_mapping_and_contract() -> None:
    assert _instrument().instrument == "SH.600000"
    assert _instrument("000001", "SZSE").instrument == "SZ.000001"
    bj = _instrument("830799", "BJSE")
    assert bj.instrument == "BJ.830799"
    assert bj.currency == "CNY"
    assert bj.tick_size == Decimal("0.01")
    assert bj.lot_size == 100


def test_input_hash_is_stable_and_binds_version() -> None:
    instrument = _instrument()
    first = CanonicalDataset(
        "v1", "Asia/Shanghai", (instrument,), (date(2025, 1, 2),), (_bar(instrument),)
    )
    same = CanonicalDataset(
        "v1", "Asia/Shanghai", (instrument,), (date(2025, 1, 2),), (_bar(instrument, "10.50"),)
    )
    changed = replace(first, data_version="v2")
    assert first.input_hash == same.input_hash
    assert first.input_hash != changed.input_hash


def test_invalid_order_and_ohlc_fail_closed() -> None:
    first = _instrument()
    second = _instrument("000001", "SZSE")
    invalid_order = CanonicalDataset("v1", "Asia/Shanghai", (second, first), (), ())
    with pytest.raises(CanonicalDataError, match="canonical order"):
        invalid_order.validate()
    with pytest.raises(CanonicalDataError, match="high below"):
        replace(_bar(first), high=Decimal("9")).validate()
