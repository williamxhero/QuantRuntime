from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import Draft202012Validator

SCHEMA_DIR = Path(__file__).parents[1] / "schemas"


@cache
def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load bundled schema {name!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"bundled schema {name!r} is not an object")
    Draft202012Validator.check_schema(value)
    return value


def validate_instance(name: str, value: Any) -> None:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = "/".join(str(item) for item in error.absolute_path) or "<root>"
        raise ValueError(f"{name} validation failed at {location}: {error.message}")
