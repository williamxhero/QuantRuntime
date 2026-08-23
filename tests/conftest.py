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
