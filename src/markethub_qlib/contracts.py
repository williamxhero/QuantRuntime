from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pandas as pd

from .canonical import sha256_value

DECISIONS_SCHEMA = "canonical-strategy-decisions.v1"
WEIGHT_QUANTUM = Decimal("0.000000000001")


def build_strategy_decisions(
    candidates: pd.DataFrame,
    *,
    strategy_spec_hash: str,
) -> dict[str, Any]:
    ranked = candidates.reset_index().sort_values(
        ["datetime", "score", "instrument"],
        ascending=[True, False, True],
        kind="stable",
    )
    decisions = [
        {
            "signal_date": pd.Timestamp(row.datetime).date().isoformat(),
            "instrument": str(row.instrument),
            "target_weight": normalize_weight(row.target_weight),
        }
        for row in ranked.itertuples(index=False)
    ]
    return {
        "schema": DECISIONS_SCHEMA,
        "strategy_spec_hash": strategy_spec_hash,
        "decisions": decisions,
    }


def decision_hash(decisions: dict[str, Any]) -> str:
    return sha256_value(decisions)


def normalize_weight(value: Any) -> str:
    weight = Decimal(str(value)).quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN)
    if weight <= 0 or weight > 1:
        raise ValueError(f"target weight must be in (0, 1], got {value!r}")
    rendered = format(weight, "f").rstrip("0").rstrip(".")
    return rendered or "0"
