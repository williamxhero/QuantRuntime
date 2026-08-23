from datetime import date
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide

from markethub_nautilus.canonical import CanonicalBar, CanonicalDataset, CanonicalInstrument
from markethub_nautilus.china_rules import AShareRuleBook, calculate_fee
from markethub_nautilus.config import Decision, FeeSpec

FEES = FeeSpec(
    Decimal("0.0003"), Decimal("5"), Decimal("0.0005"), 2, "half_away_from_zero", "per_fill"
)


def test_fee_minimum_stamp_duty_and_per_fill_rounding() -> None:
    assert calculate_fee(Decimal("1012"), OrderSide.BUY, FEES) == Decimal("5.00")
    assert calculate_fee(Decimal("1011"), OrderSide.SELL, FEES) == Decimal("5.51")
    assert calculate_fee(Decimal("100000.01"), OrderSide.SELL, FEES) == Decimal("80.00")


def test_rule_book_uses_market_lifecycle_and_price_band() -> None:
    instrument = CanonicalInstrument.from_catalog(
        {
            "code": "600000",
            "exchange": "SHSE",
            "list_date": "2025-01-02",
            "delist_date": "2025-01-31",
        }
    )
    bar = CanonicalBar(
        date(2025, 1, 3),
        instrument.instrument,
        Decimal("11"),
        Decimal("11"),
        Decimal("11"),
        Decimal("11"),
        Decimal("100"),
        Decimal("1100"),
        Decimal("10"),
        False,
        False,
    )
    dataset = CanonicalDataset("v1", "Asia/Shanghai", (instrument,), (date(2025, 1, 3),), (bar,))
    decision = Decision(
        date(2025, 1, 3), instrument.instrument, "x", 100, "buy_market_next_open", "limit_up"
    )
    assert AShareRuleBook(dataset).state_for(decision).limit_up is True
    before = Decision(
        date(2025, 1, 1), instrument.instrument, "x", 100, "buy_market_next_open", "pre_listing"
    )
    state = AShareRuleBook(dataset).state_for(before)
    assert state.before_listing is True
    assert state.has_bar is False
