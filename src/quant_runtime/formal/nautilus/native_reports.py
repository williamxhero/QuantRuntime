from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from quant_runtime.contracts.canonical_hash import canonical_json, normalize_decimal, write_json


@dataclass(slots=True)
class FormalOutput:
    framework_version: str
    data_version: str
    dataset_version: str
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
                "schema": "quant-runtime.nautilus-output.v1",
                "framework": "NautilusTrader",
                "framework_version": self.framework_version,
                "data_version": self.data_version,
                "dataset_version": self.dataset_version,
                "canonical_input_hash": self.canonical_input_hash,
                "strategy_spec_hash": self.strategy_spec_hash,
                "decision_hash": self.decision_hash,
                "decisions": self.decisions,
                "orders": self.orders,
                "rejects": self.rejects,
                "fills": self.fills,
                "positions": self.positions,
                "account_curve": self.account_curve,
                "fees": self.fees,
                "native_statistics": self.native_statistics,
            }
        )

    @property
    def output_hash(self) -> str:
        return sha256(canonical_json(self.semantic_payload())).hexdigest()


def write_normalized_output(output: Path, result: FormalOutput) -> Path:
    return write_json(
        output / "normalized_output.json",
        {
            **result.semantic_payload(),
            "metrics": normalize_value(result.metrics),
            "normalized_output_hash": result.output_hash,
        },
    )


def dataframe_records(frame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return json.loads(frame.reset_index().to_json(orient="records", date_format="iso"))


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value) if value.is_finite() else None
    if isinstance(value, float):
        decimal = Decimal(str(value))
        return normalize_decimal(decimal) if decimal.is_finite() else None
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [normalize_value(item) for item in value]
    return value
