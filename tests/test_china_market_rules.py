from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from nautilus_trader.model.enums import OrderSide

from quant_runtime.adapters.data.markethub.catalog import CanonicalInstrument
from quant_runtime.adapters.data.markethub.model import CanonicalBar, CanonicalDataset
from quant_runtime.adapters.formal.nautilus.china_market_rules import (
    AShareRuleBook,
    FeeSpec,
    RuleState,
    calculate_fee,
)
from quant_runtime.adapters.formal.nautilus.decisions import DecisionRecord
from quant_runtime.entrypoint import load_package_entrypoint


def fee_spec() -> FeeSpec:
    return FeeSpec(
        commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        currency_precision=2,
        rounding_mode="half_away_from_zero",
        rounding_scope="per_fill",
    )


def test_minimum_commission_stamp_duty_and_per_fill_rounding() -> None:
    spec = fee_spec()
    assert calculate_fee(Decimal("1000"), OrderSide.BUY, spec) == Decimal("5.00")
    assert calculate_fee(Decimal("1000"), OrderSide.SELL, spec) == Decimal("5.50")
    assert calculate_fee(Decimal("20000.01"), OrderSide.BUY, spec) == Decimal("6.00")
    assert calculate_fee(Decimal("20000.01"), OrderSide.SELL, spec) == Decimal("16.00")


def test_open_price_band_missing_bar_and_suspension_guards(canonical_dataset) -> None:
    original = next(
        item for item in canonical_dataset.bars if item.identity == (date(2025, 1, 3), "SH.600000")
    )
    limit_open = (original.pre_close * Decimal("1.10")).quantize(Decimal("0.01"))
    replaced = replace(
        original,
        open=limit_open,
        high=max(original.high, limit_open),
        is_suspended=True,
    )
    dataset = replace(
        canonical_dataset,
        bars=tuple(
            replaced if item.identity == original.identity else item
            for item in canonical_dataset.bars
        ),
    )
    rules = AShareRuleBook(dataset)
    state = rules.state_for(date(2025, 1, 3), "SH.600000", at_open=True)
    assert state.limit_up
    assert state.suspended
    assert not rules.state_for(date(2025, 1, 11), "SH.600000", at_open=True).has_bar


def test_bj_uses_thirty_percent_band() -> None:
    instrument = CanonicalInstrument(
        instrument="BJ.430001",
        raw_code="430001",
        exchange="BJSE",
        currency="CNY",
        price_precision=2,
        tick_size=Decimal("0.01"),
        lot_size=100,
        list_date=date(2020, 1, 1),
        delist_date=None,
    )
    bar = CanonicalBar(
        trading_day=date(2025, 1, 2),
        instrument=instrument.instrument,
        open=Decimal("13"),
        high=Decimal("13"),
        low=Decimal("10"),
        close=Decimal("12"),
        volume=Decimal("100"),
        amount=Decimal("1200"),
        pre_close=Decimal("10"),
        is_suspended=False,
        is_st=False,
    )
    dataset = CanonicalDataset(
        data_version="v",
        dataset_version="d",
        timezone="Asia/Shanghai",
        instruments=(instrument,),
        trading_days=(date(2025, 1, 2),),
        bars=(bar,),
    )
    assert (
        AShareRuleBook(dataset)
        .state_for(date(2025, 1, 2), instrument.instrument, at_open=True)
        .limit_up
    )


def test_canonical_package_rejects_same_day_sale_at_t_plus_one_boundary() -> None:
    package = (
        Path(__file__).parents[2]
        / "strategy-workspace"
        / "strategies"
        / "equity"
        / "cross-sectional-momentum"
    )
    strategy_class = load_package_entrypoint(
        package, "formal/nautilus/strategy.py:MomentumTopKStrategy"
    )
    trading_day = date(2025, 1, 3)
    instrument = "SH.600000"
    strategy = SimpleNamespace(
        rule_book=SimpleNamespace(state_for=lambda *_args, **_kwargs: RuleState()),
        position_quantity={instrument: 100},
        acquired_day={instrument: trading_day},
    )
    decision = DecisionRecord(trading_day, instrument, "1", Decimal("0.1"))

    assert (
        strategy_class._execution_guard(strategy, decision, trading_day, OrderSide.SELL, 100)
        == "t_plus_one"
    )
    assert (
        strategy_class._execution_guard(strategy, decision, date(2025, 1, 6), OrderSide.SELL, 100)
        is None
    )
