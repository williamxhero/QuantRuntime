from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from quant_runtime.artifacts import normalize_decimal, sha256_value

COST_RECEIPT_SCHEMA = "quant-runtime.nautilus-effective-cost-receipt.v1"
COST_RECEIPT_VERSION = 1


@dataclass(frozen=True, slots=True)
class EffectiveCostConfig:
    """The validated, effective A-share cost and sizing configuration."""

    commission_rate: Decimal
    minimum_commission_cny: Decimal
    sell_stamp_duty_rate: Decimal
    slippage_bps: Decimal
    currency_precision: int
    rounding_mode: str
    rounding_scope: str
    initial_cash_cny: Decimal
    lot_size: int
    tick_size: Decimal

    @classmethod
    def from_formal_config(cls, config: Any) -> EffectiveCostConfig:
        fees = config.fees
        result = cls(
            commission_rate=fees.commission_rate,
            minimum_commission_cny=fees.minimum_commission_cny,
            sell_stamp_duty_rate=fees.sell_stamp_duty_rate,
            slippage_bps=config.slippage_bps,
            currency_precision=fees.currency_precision,
            rounding_mode=fees.rounding_mode,
            rounding_scope=fees.rounding_scope,
            initial_cash_cny=config.initial_cash_cny,
            lot_size=config.lot_size,
            tick_size=config.tick_size,
        )
        result.validate()
        return result

    def validate(self) -> None:
        decimals = (
            self.commission_rate,
            self.minimum_commission_cny,
            self.sell_stamp_duty_rate,
            self.slippage_bps,
            self.initial_cash_cny,
            self.tick_size,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimals):
            raise ValueError("effective cost values must be finite decimals")
        if (
            min(
                self.commission_rate,
                self.minimum_commission_cny,
                self.sell_stamp_duty_rate,
                self.slippage_bps,
            )
            < 0
        ):
            raise ValueError("effective cost rates and slippage must be non-negative")
        if self.initial_cash_cny <= 0:
            raise ValueError("effective initial cash must be positive")
        if self.currency_precision != 2:
            raise ValueError("effective cost currency precision must be 2")
        if self.rounding_mode != "half_away_from_zero":
            raise ValueError("effective cost rounding mode is unsupported")
        if self.rounding_scope != "per_fill":
            raise ValueError("effective cost rounding scope is unsupported")
        if self.lot_size != 100 or self.tick_size != Decimal("0.01"):
            raise ValueError("effective A-share sizing must use 100-share lots and 0.01 ticks")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "commission_rate": normalize_decimal(self.commission_rate),
            "minimum_commission_cny": normalize_decimal(self.minimum_commission_cny),
            "sell_stamp_duty_rate": normalize_decimal(self.sell_stamp_duty_rate),
            "slippage_bps": normalize_decimal(self.slippage_bps),
            "currency_precision": self.currency_precision,
            "rounding_mode": self.rounding_mode,
            "rounding_scope": self.rounding_scope,
            "initial_cash_cny": normalize_decimal(self.initial_cash_cny),
            "lot_size": self.lot_size,
            "tick_size": normalize_decimal(self.tick_size),
        }


def build_cost_receipt(
    effective: EffectiveCostConfig,
    *,
    request_hash: str,
    package_hash: str,
    formal_phase_id: str,
    runtime_image_identity: str,
    runtime_lock_identity: str,
) -> dict[str, Any]:
    effective_payload = effective.as_dict()
    binding = {
        "request_hash": _hash(request_hash, "request_hash"),
        "package_hash": _hash(package_hash, "package_hash"),
        "formal_phase_id": _nonempty(formal_phase_id, "formal_phase_id"),
        "runtime_image_identity": _runtime_identity(
            runtime_image_identity, "runtime_image_identity"
        ),
        "runtime_lock_identity": _runtime_identity(runtime_lock_identity, "runtime_lock_identity"),
    }
    canonical_payload = {
        "schema": COST_RECEIPT_SCHEMA,
        "version": COST_RECEIPT_VERSION,
        "effective": effective_payload,
    }
    return {
        **canonical_payload,
        "canonical_hash": sha256_value(canonical_payload),
        "binding": binding,
    }


def verify_cost_receipt(
    receipt: Mapping[str, Any],
    *,
    request_hash: str,
    package_hash: str,
    formal_phase_id: str,
    runtime_image_identity: str,
    runtime_lock_identity: str,
) -> dict[str, Any]:
    """Validate a receipt and its immutable execution binding before publication."""
    value = {str(key): item for key, item in receipt.items()}
    if set(value) != {"schema", "version", "effective", "canonical_hash", "binding"}:
        raise ValueError("cost receipt shape is invalid")
    if value["schema"] != COST_RECEIPT_SCHEMA or value["version"] != COST_RECEIPT_VERSION:
        raise ValueError("cost receipt schema is unsupported")
    effective = value["effective"]
    if not isinstance(effective, Mapping):
        raise ValueError("cost receipt effective configuration must be an object")
    normalized_effective = _normalized_effective(effective)
    canonical_payload = {
        "schema": COST_RECEIPT_SCHEMA,
        "version": COST_RECEIPT_VERSION,
        "effective": normalized_effective,
    }
    if value["canonical_hash"] != sha256_value(canonical_payload):
        raise ValueError("cost receipt canonical hash mismatch")
    expected_binding = {
        "request_hash": _hash(request_hash, "request_hash"),
        "package_hash": _hash(package_hash, "package_hash"),
        "formal_phase_id": _nonempty(formal_phase_id, "formal_phase_id"),
        "runtime_image_identity": _runtime_identity(
            runtime_image_identity, "runtime_image_identity"
        ),
        "runtime_lock_identity": _runtime_identity(runtime_lock_identity, "runtime_lock_identity"),
    }
    if value["binding"] != expected_binding:
        raise ValueError("cost receipt execution binding mismatch")
    return {
        **canonical_payload,
        "canonical_hash": value["canonical_hash"],
        "binding": expected_binding,
    }


def _normalized_effective(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "commission_rate",
        "minimum_commission_cny",
        "sell_stamp_duty_rate",
        "slippage_bps",
        "currency_precision",
        "rounding_mode",
        "rounding_scope",
        "initial_cash_cny",
        "lot_size",
        "tick_size",
    }
    if set(value) != required:
        raise ValueError("cost receipt effective configuration fields are invalid")
    decimals = {
        name: _decimal(value[name], name)
        for name in (
            "commission_rate",
            "minimum_commission_cny",
            "sell_stamp_duty_rate",
            "slippage_bps",
            "initial_cash_cny",
            "tick_size",
        )
    }
    result = {
        **{name: normalize_decimal(item) for name, item in decimals.items()},
        "currency_precision": _integer(value["currency_precision"], "currency_precision"),
        "rounding_mode": _nonempty(value["rounding_mode"], "rounding_mode"),
        "rounding_scope": _nonempty(value["rounding_scope"], "rounding_scope"),
        "lot_size": _integer(value["lot_size"], "lot_size"),
    }
    effective = EffectiveCostConfig(
        commission_rate=decimals["commission_rate"],
        minimum_commission_cny=decimals["minimum_commission_cny"],
        sell_stamp_duty_rate=decimals["sell_stamp_duty_rate"],
        slippage_bps=decimals["slippage_bps"],
        currency_precision=result["currency_precision"],
        rounding_mode=result["rounding_mode"],
        rounding_scope=result["rounding_scope"],
        initial_cash_cny=decimals["initial_cash_cny"],
        lot_size=result["lot_size"],
        tick_size=decimals["tick_size"],
    )
    return effective.as_dict()


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"cost receipt {name} must be a decimal")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"cost receipt {name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"cost receipt {name} must be finite")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"cost receipt {name} must be an integer")
    return int(value)


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"cost receipt {name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    rendered = _nonempty(value, name)
    if len(rendered) != 64 or any(char not in "0123456789abcdef" for char in rendered):
        raise ValueError(f"cost receipt {name} must be a lowercase sha256")
    return rendered


def _runtime_identity(value: Any, name: str) -> str:
    rendered = _nonempty(value, name)
    if not rendered.startswith("sha256:"):
        raise ValueError(f"cost receipt {name} must be a sha256 identity")
    digest = rendered.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"cost receipt {name} must be a sha256 identity")
    return rendered
