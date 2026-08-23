from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from markethub_nautilus.canonical import CanonicalBar, CanonicalDataset, CanonicalInstrument
from markethub_nautilus.config import RunConfig

ROOT = Path(__file__).parents[1]
TRADING_DAYS = tuple(
    date.fromisoformat(value)
    for value in (
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
        "2025-01-07",
        "2025-01-08",
        "2025-01-09",
        "2025-01-10",
        "2025-01-13",
        "2025-01-14",
        "2025-01-15",
        "2025-01-16",
        "2025-01-17",
        "2025-01-20",
        "2025-01-21",
        "2025-01-22",
        "2025-01-23",
        "2025-01-24",
        "2025-01-27",
    )
)


@pytest.fixture
def s_config() -> RunConfig:
    return RunConfig.load(ROOT / "configs" / "s-validation.json")


@pytest.fixture
def s_dataset() -> CanonicalDataset:
    instruments = (
        CanonicalInstrument.from_catalog(
            {"code": "600000", "exchange": "SHSE", "list_date": "1999-11-10"}
        ),
        CanonicalInstrument.from_catalog(
            {"code": "000001", "exchange": "SZSE", "list_date": "1991-04-03"}
        ),
    )
    open_prices = {
        (date(2025, 1, 3), "SH.600000"): Decimal("10.12"),
        (date(2025, 1, 7), "SH.600000"): Decimal("10.11"),
        (date(2025, 1, 8), "SH.600000"): Decimal("10.26"),
        (date(2025, 1, 13), "SH.600000"): Decimal("10.06"),
        (date(2025, 1, 16), "SZ.000001"): Decimal("10.62"),
        (date(2025, 1, 21), "SZ.000001"): Decimal("10.53"),
    }
    bars = []
    for trading_day in TRADING_DAYS:
        for instrument in instruments:
            open_price = open_prices.get((trading_day, instrument.instrument), Decimal("10.00"))
            close = Decimal("10.00")
            high = max(open_price, close) + Decimal("0.10")
            low = min(open_price, close) - Decimal("0.10")
            bars.append(
                CanonicalBar(
                    trading_day=trading_day,
                    instrument=instrument.instrument,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal("1000000"),
                    amount=Decimal("10000000"),
                    pre_close=Decimal("10.00"),
                    is_suspended=False,
                    is_st=False,
                )
            )
    result = CanonicalDataset(
        data_version="synthetic-s-rule-seam-v1",
        timezone="Asia/Shanghai",
        instruments=instruments,
        trading_days=TRADING_DAYS,
        bars=tuple(bars),
    )
    result.validate()
    return result


@pytest.fixture
def momentum_s_dataset() -> CanonicalDataset:
    instruments = (
        CanonicalInstrument.from_catalog(
            {"code": "600000", "exchange": "SHSE", "list_date": "1999-11-10"}
        ),
        CanonicalInstrument.from_catalog(
            {"code": "000001", "exchange": "SZSE", "list_date": "1991-04-03"}
        ),
    )
    days = tuple(
        date.fromisoformat(value)
        for value in (
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
            "2025-01-08",
            "2025-01-09",
            "2025-01-10",
        )
    )
    prices = {
        "SH.600000": (
            ("20.0", "20.1", "19.7", "19.8"),
            ("19.8", "20.0", "19.6", "19.9"),
            ("19.9", "20.0", "19.4", "19.5"),
            ("19.5", "19.9", "19.4", "19.8"),
            ("19.8", "19.9", "19.2", "19.3"),
            ("19.3", "19.8", "19.2", "19.7"),
            ("19.7", "19.8", "19.0", "19.1"),
        ),
        "SZ.000001": (
            ("10.0", "10.2", "9.9", "10.1"),
            ("10.1", "10.4", "10.0", "10.3"),
            ("10.3", "10.6", "10.2", "10.5"),
            ("10.5", "10.7", "10.4", "10.6"),
            ("10.6", "10.9", "10.5", "10.8"),
            ("10.8", "11.0", "10.7", "10.9"),
            ("10.9", "11.2", "10.8", "11.1"),
        ),
    }
    bars = []
    for index, trading_day in enumerate(days):
        for instrument in instruments:
            open_price, high, low, close = (
                Decimal(value) for value in prices[instrument.instrument][index]
            )
            bars.append(
                CanonicalBar(
                    trading_day=trading_day,
                    instrument=instrument.instrument,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal("1000"),
                    amount=close * Decimal("1000"),
                    pre_close=(
                        Decimal(prices[instrument.instrument][index - 1][3])
                        if index
                        else open_price
                    ),
                    is_suspended=False,
                    is_st=False,
                )
            )
    result = CanonicalDataset(
        data_version="fixture-global-v1",
        timezone="Asia/Shanghai",
        instruments=instruments,
        trading_days=days,
        bars=tuple(bars),
    )
    result.validate()
    return result
