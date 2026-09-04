"""Strict bounded transport metadata for the Quant Runtime CLI."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from quant_runtime.artifacts import sha256_value

MAX_TRANSPORT_BYTES = 2 * 1024 * 1024


class TransportContractError(ValueError):
    pass


def read_transport_json(path: Path) -> dict[str, Any]:
    """Read one regular non-link UTF-8 JSON object exactly once."""

    try:
        link_metadata = path.lstat()
        attributes = getattr(link_metadata, "st_file_attributes", 0)
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise TransportContractError("transport input must be a regular non-link file")
        if link_metadata.st_size > MAX_TRANSPORT_BYTES:
            raise TransportContractError("transport input exceeds the size limit")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise TransportContractError("transport input must be a regular non-link file")
            chunks: list[bytes] = []
            remaining = MAX_TRANSPORT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
    except TransportContractError:
        raise
    except OSError as exc:
        raise TransportContractError("transport input is unavailable") from exc
    if len(content) > MAX_TRANSPORT_BYTES:
        raise TransportContractError("transport input exceeds the size limit")
    if content.startswith(b"\xef\xbb\xbf"):
        raise TransportContractError("transport input must not contain a BOM")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TransportContractError("transport input must be UTF-8") from exc

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise TransportContractError("transport input contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise TransportContractError(f"transport input contains non-finite value {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise TransportContractError("transport input is not strict JSON") from exc
    if not isinstance(value, dict):
        raise TransportContractError("transport input root must be an object")
    return {str(key): item for key, item in value.items()}


def validate_binding(
    binding: dict[str, Any],
    request: dict[str, Any],
    preflight: dict[str, Any],
    capability_id: str,
) -> None:
    required = {
        "schema",
        "protocol_id",
        "cell_id",
        "request_sha256",
        "preflight_sha256",
        "runtime_capability_id",
        "binding_id",
    }
    if set(binding) != required or binding.get("schema") != "quant-runtime.validation-binding.v1":
        raise TransportContractError("validation binding fields are invalid")
    for name in (
        "protocol_id",
        "cell_id",
        "request_sha256",
        "preflight_sha256",
        "runtime_capability_id",
        "binding_id",
    ):
        value = binding.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise TransportContractError("validation binding identity is invalid")
    if binding["request_sha256"] != sha256_value(request):
        raise TransportContractError("validation request digest mismatch")
    if binding["preflight_sha256"] != sha256_value(preflight):
        raise TransportContractError("validation preflight digest mismatch")
    if binding["runtime_capability_id"] != capability_id:
        raise TransportContractError("Runtime capability identity mismatch")
    identity = {key: value for key, value in binding.items() if key != "binding_id"}
    if binding["binding_id"] != sha256_value(identity):
        raise TransportContractError("validation binding identity mismatch")
