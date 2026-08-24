from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from quant_runtime.artifacts import normalize_decimal, sha256_value

DECISIONS_SCHEMA = "quant-runtime.nautilus-observed-decisions.v1"
WEIGHT_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    signal_date: date
    instrument: str
    target_weight: str
    score: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "signal_date": self.signal_date.isoformat(),
            "instrument": self.instrument,
            "target_weight": self.target_weight,
        }


@dataclass(frozen=True, slots=True)
class FormalDecisionRecord:
    """Engine-observed package decision with an intent-specific canonical payload."""

    ts_event: int
    instrument: str
    intent: str
    payload: dict[str, Any]

    def validate(self) -> None:
        if self.ts_event < 0 or not self.instrument or not self.intent:
            raise ValueError("formal decision requires timestamp, instrument, and intent")
        if self.intent not in {
            "target_weight",
            "target_position",
            "target_contracts",
            "target_notional",
            "order",
            "cancel",
            "spread",
        }:
            raise ValueError(f"unsupported formal decision intent {self.intent!r}")
        if not isinstance(self.payload, dict) or not self.payload:
            raise ValueError("formal decision payload must be a non-empty object")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "ts_event": self.ts_event,
            "instrument": self.instrument,
            "intent": self.intent,
            "payload": self.payload,
        }


def decision_envelope(
    decisions: list[DecisionRecord | FormalDecisionRecord]
    | tuple[DecisionRecord | FormalDecisionRecord, ...],
    strategy_identity_hash: str,
    *,
    generic: bool = False,
) -> dict[str, Any]:
    if generic:
        if any(not isinstance(item, FormalDecisionRecord) for item in decisions):
            raise ValueError("generic formal decisions require FormalDecisionRecord values")
        ordered = sorted(
            decisions,
            key=lambda item: (item.ts_event, item.instrument, item.intent),  # type: ignore[union-attr]
        )
        schema = "quant-runtime.nautilus-observed-decisions.v2"
    elif all(isinstance(item, DecisionRecord) for item in decisions):
        ordered = sorted(
            decisions,
            key=lambda item: (item.signal_date, -item.score, item.instrument),  # type: ignore[union-attr]
        )
        schema = DECISIONS_SCHEMA
    else:
        raise ValueError("formal decisions cannot mix legacy and generic record types")
    return {
        "schema": schema,
        "strategy_identity_hash": strategy_identity_hash,
        "observed_by": "NautilusTrader",
        "decisions": [item.as_dict() for item in ordered],
    }


def decision_hash(envelope: dict[str, Any]) -> str:
    return sha256_value(envelope)


def canonical_weight(count: int) -> str:
    if count < 1:
        raise ValueError("cannot assign weights to an empty selection")
    value = (Decimal(1) / Decimal(count)).quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN)
    return normalize_decimal(value)
