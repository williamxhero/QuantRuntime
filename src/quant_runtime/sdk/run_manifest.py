from __future__ import annotations

from typing import Any

from .schema import validate_instance


def validate_run_request(value: dict[str, Any]) -> dict[str, Any]:
    validate_instance("workspace-run-request.v1", value)
    return value


def validate_run_manifest(value: dict[str, Any]) -> dict[str, Any]:
    validate_instance("run-manifest.v1", value)
    return value
