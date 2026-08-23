from __future__ import annotations

from typing import Any

from .schema import validate_instance


def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    validate_instance("result.v1", value)
    return value
