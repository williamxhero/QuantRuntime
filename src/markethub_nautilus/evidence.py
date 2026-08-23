from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from typing import Any

from .canonical import canonical_json, normalize_decimal


@dataclass(slots=True)
class NormalizedOutput:
    framework_version: str
    data_version: str
    canonical_input_hash: str
    strategy_spec_hash: str
    decision_hash: str
    decisions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    rejects: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    account_curve: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    native_statistics: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def semantic_payload(self) -> dict[str, Any]:
        return normalize_value(
            {
                "account_curve": self.account_curve,
                "canonical_input_hash": self.canonical_input_hash,
                "data_version": self.data_version,
                "decision_hash": self.decision_hash,
                "decisions": self.decisions,
                "fees": self.fees,
                "fills": self.fills,
                "framework": "nautilus_trader",
                "framework_version": self.framework_version,
                "native_statistics": self.native_statistics,
                "orders": self.orders,
                "positions": self.positions,
                "rejects": self.rejects,
                "schema": "markethub-nautilus.normalized-output.v1",
                "strategy_spec_hash": self.strategy_spec_hash,
            }
        )

    @property
    def output_hash(self) -> str:
        return sha256(canonical_json(self.semantic_payload())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "metrics": normalize_value(self.metrics),
            "normalized_output_hash": self.output_hash,
        }


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value) if value.is_finite() else None
    if isinstance(value, float):
        converted = Decimal(str(value))
        return normalize_decimal(converted) if converted.is_finite() else None
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [normalize_value(item) for item in value]
    return value
