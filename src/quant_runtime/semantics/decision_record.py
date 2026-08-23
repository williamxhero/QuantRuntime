from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from quant_runtime.contracts.canonical_hash import normalize_decimal, sha256_value

DECISIONS_SCHEMA = "canonical-strategy-decisions.v1"
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


def decision_envelope(
    decisions: list[DecisionRecord] | tuple[DecisionRecord, ...],
    strategy_spec_hash: str,
) -> dict[str, Any]:
    ordered = sorted(decisions, key=lambda item: (item.signal_date, -item.score, item.instrument))
    return {
        "schema": DECISIONS_SCHEMA,
        "strategy_spec_hash": strategy_spec_hash,
        "decisions": [item.as_dict() for item in ordered],
    }


def decision_hash(envelope: dict[str, Any]) -> str:
    return sha256_value(envelope)


def canonical_weight(count: int) -> str:
    if count < 1:
        raise ValueError("cannot assign weights to an empty selection")
    value = (Decimal(1) / Decimal(count)).quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN)
    return normalize_decimal(value)
