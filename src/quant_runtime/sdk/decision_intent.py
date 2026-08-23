from __future__ import annotations

from typing import Any

from quant_runtime.contracts.canonical_hash import sha256_value

from .schema import validate_instance

DECISION_INTENTS_SCHEMA = "quant-research.decision-intents.v2"


def validate_decision_intents(value: dict[str, Any]) -> dict[str, Any]:
    validate_instance("decision-intents.v2", value)
    if value.get("schema") != DECISION_INTENTS_SCHEMA:
        raise ValueError(f"unsupported decision intents schema {value.get('schema')!r}")
    return value


def decision_intents_hash(value: dict[str, Any]) -> str:
    return sha256_value(validate_decision_intents(value))
