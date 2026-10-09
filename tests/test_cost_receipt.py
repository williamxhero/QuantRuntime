from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.model.enums import OrderSide

from quant_runtime.adapters.formal.nautilus.adapter import _build_cost_receipt, _formal_config
from quant_runtime.adapters.formal.nautilus.china_market_rules import FeeSpec, calculate_fee
from quant_runtime.adapters.formal.nautilus.cost_receipt import (
    COST_RECEIPT_SCHEMA,
    EffectiveCostConfig,
    build_cost_receipt,
    verify_cost_receipt,
)
from quant_runtime.adapters.formal.nautilus.native_reports import FormalOutput
from quant_runtime.adapters.interface import FormalRunInput
from quant_runtime.package import StrategyPackage

HASH = "a" * 64
IMAGE = "sha256:" + "b" * 64
LOCK = "sha256:" + "c" * 64


def _formal_input(config: dict[str, Any]) -> FormalRunInput:
    package = StrategyPackage(
        Path("."),
        {"strategy_id": "fixture", "revision": 1, "package_hash": HASH},
        {"strategy_id": "fixture", "revision": 1},
    )
    return FormalRunInput(
        package=package,
        parameters={},
        snapshot=cast(Any, None),
        output=Path("."),
        config=config,
        cache_path=None,
        cache_policy="none",
        cache_transform_version=None,
    )


def _resolved(config: dict[str, Any] | None = None) -> EffectiveCostConfig:
    return EffectiveCostConfig.from_formal_config(_formal_config(_formal_input(config or {})))


def _receipt(effective: EffectiveCostConfig | None = None) -> dict[str, Any]:
    return build_cost_receipt(
        effective or _resolved(),
        request_hash=HASH,
        package_hash="d" * 64,
        formal_phase_id="primary",
        runtime_image_identity=IMAGE,
        runtime_lock_identity=LOCK,
    )


def test_empty_config_receipt_publishes_resolved_defaults() -> None:
    effective = _resolved()
    assert effective.commission_rate == Decimal("0.0003")
    assert effective.minimum_commission_cny == Decimal("5.00")
    assert effective.sell_stamp_duty_rate == Decimal("0.0005")
    assert effective.slippage_bps == Decimal("0")
    assert effective.currency_precision == 2
    assert effective.rounding_mode == "half_away_from_zero"
    assert effective.rounding_scope == "per_fill"
    assert effective.initial_cash_cny == Decimal("1000000.00")
    assert effective.lot_size == 100
    assert effective.tick_size == Decimal("0.01")


def test_direct_adapter_receipt_is_local_only_without_binding() -> None:
    value = _formal_input({})
    config = _formal_config(value)

    assert _build_cost_receipt(value, config, "primary") is None


def test_partial_adapter_binding_fails_closed() -> None:
    value = replace(_formal_input({}), request_hash=HASH)
    config = _formal_config(value)

    with pytest.raises(ValueError, match="runtime image and lock identity"):
        _build_cost_receipt(value, config, "primary")


def test_explicit_config_is_normalized_and_receipt_is_bound() -> None:
    effective = _resolved(
        {
            "initial_cash_cny": "2500000.00",
            "lot_size": 100,
            "tick_size": "0.010",
            "slippage_bps": "12.50",
            "fees": {
                "commission_rate": "0.0003000",
                "minimum_commission_cny": "5.000",
                "sell_stamp_duty_rate": "0.0005000",
                "currency_precision": 2,
                "rounding_mode": "half_away_from_zero",
                "rounding_scope": "per_fill",
            },
        }
    )
    receipt = _receipt(effective)
    assert receipt["schema"] == COST_RECEIPT_SCHEMA
    assert receipt["version"] == 1
    assert receipt["effective"]["slippage_bps"] == "12.5"
    assert receipt["effective"]["tick_size"] == "0.01"
    assert receipt["binding"]["request_hash"] == HASH
    assert receipt["binding"]["package_hash"] == "d" * 64
    assert receipt["binding"]["formal_phase_id"] == "primary"
    assert receipt["binding"]["runtime_image_identity"] == IMAGE
    assert receipt["binding"]["runtime_lock_identity"] == LOCK
    assert (
        verify_cost_receipt(
            receipt,
            request_hash=HASH,
            package_hash="d" * 64,
            formal_phase_id="primary",
            runtime_image_identity=IMAGE,
            runtime_lock_identity=LOCK,
        )
        == receipt
    )


def test_equivalent_decimal_spellings_have_stable_identity() -> None:
    first = _receipt()
    second = _receipt(
        EffectiveCostConfig(
            commission_rate=Decimal("0.00030"),
            minimum_commission_cny=Decimal("5.000"),
            sell_stamp_duty_rate=Decimal("0.000500"),
            slippage_bps=Decimal("0.00"),
            currency_precision=2,
            rounding_mode="half_away_from_zero",
            rounding_scope="per_fill",
            initial_cash_cny=Decimal("1000000.000"),
            lot_size=100,
            tick_size=Decimal("0.010"),
        )
    )
    assert first["canonical_hash"] == second["canonical_hash"]
    assert first["effective"] == second["effective"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commission_rate", "NaN"),
        ("minimum_commission_cny", "-0.01"),
        ("sell_stamp_duty_rate", "Infinity"),
        ("slippage_bps", "-1"),
        ("currency_precision", 3),
        ("rounding_mode", "half_even"),
        ("rounding_scope", "per_order"),
        ("lot_size", 1),
        ("tick_size", "0.001"),
    ],
)
def test_invalid_effective_values_are_rejected(field: str, value: Any) -> None:
    values = {
        "commission_rate": Decimal("0.0003"),
        "minimum_commission_cny": Decimal("5"),
        "sell_stamp_duty_rate": Decimal("0.0005"),
        "slippage_bps": Decimal("0"),
        "currency_precision": 2,
        "rounding_mode": "half_away_from_zero",
        "rounding_scope": "per_fill",
        "initial_cash_cny": Decimal("1000000"),
        "lot_size": 100,
        "tick_size": Decimal("0.01"),
    }
    values[field] = value
    with pytest.raises(ValueError):
        build_cost_receipt(
            EffectiveCostConfig(**values),
            request_hash=HASH,
            package_hash=HASH,
            formal_phase_id="primary",
            runtime_image_identity=IMAGE,
            runtime_lock_identity=LOCK,
        )


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": 1},
        {"fees": {"unknown": 1}},
        {"fees": {"commission_rate": True}},
        {"fees": {"currency_precision": True}},
        {"market_data": {"unknown": "none"}},
        {"initial_cash_cny": "NaN"},
        {"slippage_bps": -1},
    ],
)
def test_malformed_or_unknown_request_config_is_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _formal_config(_formal_input(config))


def test_legacy_request_score_metadata_does_not_change_effective_cost() -> None:
    value = replace(
        _formal_input({"score": 2.0}),
        request_schema="quant-research.workspace-run-request.v2",
    )

    assert _formal_config(value, allow_legacy_metadata=True) == _formal_config(_formal_input({}))


def test_minimum_commission_and_sell_only_stamp_duty() -> None:
    spec = FeeSpec(
        commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        currency_precision=2,
        rounding_mode="half_away_from_zero",
        rounding_scope="per_fill",
    )
    assert calculate_fee(Decimal("100"), OrderSide.BUY, spec) == Decimal("5.00")
    assert calculate_fee(Decimal("100"), OrderSide.SELL, spec) == Decimal("5.05")
    assert calculate_fee(Decimal("100000"), OrderSide.BUY, spec) == Decimal("30.00")
    assert calculate_fee(Decimal("100000"), OrderSide.SELL, spec) == Decimal("80.00")


def test_per_fill_half_away_from_zero_rounding_is_effective() -> None:
    spec = FeeSpec(
        commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("0"),
        sell_stamp_duty_rate=Decimal("0"),
        currency_precision=2,
        rounding_mode="half_away_from_zero",
        rounding_scope="per_fill",
    )
    assert calculate_fee(Decimal("16.683333333333333333"), OrderSide.BUY, spec) == Decimal("0.01")
    assert calculate_fee(Decimal("16.65"), OrderSide.BUY, spec) == Decimal("0.00")


def test_tampered_effective_payload_is_rejected() -> None:
    tampered = deepcopy(_receipt())
    tampered["effective"]["slippage_bps"] = "99"
    with pytest.raises(ValueError, match="canonical hash"):
        verify_cost_receipt(
            tampered,
            request_hash=HASH,
            package_hash="d" * 64,
            formal_phase_id="primary",
            runtime_image_identity=IMAGE,
            runtime_lock_identity=LOCK,
        )


@pytest.mark.parametrize(
    "binding",
    [
        "request_hash",
        "package_hash",
        "formal_phase_id",
        "runtime_image_identity",
        "runtime_lock_identity",
    ],
)
def test_tampered_binding_is_rejected(binding: str) -> None:
    tampered = deepcopy(_receipt())
    tampered["binding"][binding] = "e" * 64
    with pytest.raises(ValueError, match="binding"):
        verify_cost_receipt(
            tampered,
            request_hash=HASH,
            package_hash="d" * 64,
            formal_phase_id="primary",
            runtime_image_identity=IMAGE,
            runtime_lock_identity=LOCK,
        )


def test_legacy_normalized_output_remains_readable_without_receipt() -> None:
    legacy = FormalOutput(
        framework_version="1.231.0",
        data_version="fixture",
        dataset_version="fixture",
        canonical_input_hash=HASH,
        strategy_spec_hash=HASH,
        decision_hash=HASH,
    )
    payload = legacy.semantic_payload()
    assert payload["schema"] == "quant-runtime.nautilus-output.v1"
    assert "cost_receipt" not in payload
    assert "normalized_output_hash" not in payload
