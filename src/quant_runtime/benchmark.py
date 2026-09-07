"""Strict transport-only execution for external research benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from quant_runtime.artifacts import sha256_value
from quant_runtime.sandbox.invocation import CancellationToken, PreparedSandboxInvocation
from quant_runtime.sandbox.oci import OciSandboxBackend
from quant_runtime.transport import TransportContractError, read_transport_json

MAX_SOURCE_BYTES = 2**24
MAX_FIXTURE_BYTES = 2**31 - 1
MAX_RESULT_BYTES = 2**21


class BenchmarkBackend(Protocol):
    def invoke(self, prepared: PreparedSandboxInvocation) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BenchmarkCodeMount:
    root: Path


@dataclass(frozen=True, slots=True)
class BlobIdentity:
    sha256: str
    bytes: int
    media_type: str


class BenchmarkExecutionService:
    """Validate frozen bytes and invoke the existing containment backend exactly once."""

    def __init__(self, backend: BenchmarkBackend) -> None:
        self._backend = backend

    def execute(self, request_path: Path, source_path: Path, fixture_path: Path) -> dict[str, Any]:
        request = _request(read_transport_json(request_path))
        limits = request["transport_limits"]
        source_identity = _blob_identity(request["source"], "text/x-python")
        strategy_trace = request["schema"] == "quant-runtime.benchmark-exec-request.v2"
        input_name = "scenario" if strategy_trace else "fixture"
        input_limit = "scenario_bytes" if strategy_trace else "fixture_bytes"
        fixture_identity = _blob_identity(request[input_name], "application/json")
        source = _read_blob(source_path, source_identity, "source", limits["source_bytes"])
        fixture = _read_blob(fixture_path, fixture_identity, input_name, limits[input_limit])
        _strict_json_object(fixture, label=f"benchmark {input_name}")
        if request["execution_mode"] == "production_attested_oci" and not isinstance(
            self._backend, OciSandboxBackend
        ):
            return _blocked(request, "benchmark_oci_unavailable")
        entrypoint = str(request["entrypoint"])
        filename, _separator, callable_name = entrypoint.partition(":")
        with TemporaryDirectory(prefix="quant-runtime-benchmark-") as temporary:
            root = Path(temporary)
            code_root = root / "code"
            inputs = root / "inputs"
            output = root / "output"
            code_root.mkdir()
            inputs.mkdir()
            output.mkdir()
            (code_root / filename).write_bytes(source)
            input_filename = "scenario.json" if strategy_trace else "fixture.json"
            (inputs / input_filename).write_bytes(fixture)
            protocol_identity = {
                "schema": "quant-runtime.sandbox-invocation.v1",
                "phase": "benchmark_strategy" if strategy_trace else "benchmark_factor",
                "benchmark_request_id": request["invocation_id"],
                "source": request["source"],
                input_name: request[input_name],
                "entrypoint": entrypoint,
                "callable": callable_name,
                "sandbox_profile": request["sandbox_profile"],
                "mounts": {
                    "package": "/sandbox/package",
                    "inputs": "/sandbox/inputs",
                    "output": "/sandbox/output",
                },
            }
            if strategy_trace:
                protocol_identity["workload_kind"] = request["workload_kind"]
            protocol = {
                **protocol_identity,
                "invocation_id": "sha256:" + sha256_value(protocol_identity),
            }
            prepared = PreparedSandboxInvocation(
                protocol=protocol,
                package=BenchmarkCodeMount(code_root),
                inputs=inputs,
                output=output,
                cancellation=CancellationToken(),
            )
            result = _worker_result(
                self._backend.invoke(prepared),
                protocol["invocation_id"],
                limits["result_bytes"],
            )
        status = "completed" if result["classification"] == "success" else "failed"
        identity = {
            "schema": (
                "quant-runtime.benchmark-exec-result.v2"
                if strategy_trace
                else "quant-runtime.benchmark-exec-result.v1"
            ),
            "status": status,
            "benchmark_invocation_id": request["invocation_id"],
            "sandbox_invocation_id": protocol["invocation_id"],
            "classification": result["classification"],
            "payload": result["payload"],
            "sandbox": result["sandbox"],
        }
        return {**identity, "execution_id": sha256_value(identity)}


def _request(value: dict[str, Any]) -> dict[str, Any]:
    schema = value.get("schema")
    common = {
        "schema",
        "invocation_id",
        "execution_mode",
        "source",
        "entrypoint",
        "sandbox_profile",
        "transport_limits",
    }
    if schema == "quant-runtime.benchmark-exec-request.v1":
        required = common | {"fixture"}
        input_limit = "fixture_bytes"
    elif schema == "quant-runtime.benchmark-exec-request.v2":
        required = common | {"workload_kind", "scenario"}
        input_limit = "scenario_bytes"
        if value.get("workload_kind") != "strategy_event_trace":
            raise TransportContractError("benchmark workload kind is invalid")
    else:
        raise TransportContractError("benchmark execution request fields are invalid")
    if set(value) != required:
        raise TransportContractError("benchmark execution request fields are invalid")
    if value.get("execution_mode") not in {"contract_fake", "production_attested_oci"}:
        raise TransportContractError("benchmark execution mode is invalid")
    entrypoint = value.get("entrypoint")
    if (
        not isinstance(entrypoint, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,124}\.py:[A-Za-z_][A-Za-z0-9_]*", entrypoint)
        is None
    ):
        raise TransportContractError("benchmark entrypoint is invalid")
    filename = entrypoint.partition(":")[0]
    if Path(filename).name != filename:
        raise TransportContractError("benchmark entrypoint must be a single transport filename")
    if not isinstance(value.get("sandbox_profile"), dict):
        raise TransportContractError("benchmark sandbox profile must be an object")
    limits = value.get("transport_limits")
    if (
        not isinstance(limits, dict)
        or set(limits) != {"source_bytes", input_limit, "result_bytes"}
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1
            for item in limits.values()
        )
        or limits["source_bytes"] > MAX_SOURCE_BYTES
        or limits[input_limit] > MAX_FIXTURE_BYTES
        or limits["result_bytes"] > MAX_RESULT_BYTES
    ):
        raise TransportContractError("benchmark transport limits are invalid")
    identity = {key: item for key, item in value.items() if key != "invocation_id"}
    if value.get("invocation_id") != sha256_value(identity):
        raise TransportContractError("benchmark execution request identity mismatch")
    return value


def _blob_identity(value: object, media_type: str) -> BlobIdentity:
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes", "media_type"}:
        raise TransportContractError("benchmark blob identity fields are invalid")
    digest = value.get("sha256")
    size = value.get("bytes")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or value.get("media_type") != media_type
    ):
        raise TransportContractError("benchmark blob identity is invalid")
    return BlobIdentity(digest, size, media_type)


def _read_blob(path: Path, identity: BlobIdentity, label: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            path.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > maximum
        ):
            raise TransportContractError(f"benchmark {label} must be a bounded regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise TransportContractError(f"benchmark {label} must be a regular file")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
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
        raise TransportContractError(f"benchmark {label} is unavailable") from exc
    if len(content) != identity.bytes or hashlib.sha256(content).hexdigest() != identity.sha256:
        raise TransportContractError(f"benchmark {label} identity mismatch")
    return content


def _strict_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise TransportContractError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    def reject(value: str) -> None:
        raise TransportContractError(f"{label} contains non-finite value {value}")

    try:
        parsed = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TransportContractError(f"{label} is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise TransportContractError(f"{label} root must be an object")
    return parsed


def _worker_result(
    value: Mapping[str, Any], invocation_id: str, maximum_bytes: int
) -> dict[str, Any]:
    mapped = {str(key): item for key, item in value.items()}
    try:
        encoded = json.dumps(mapped, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise TransportContractError("benchmark sandbox result is invalid") from exc
    if len(encoded) > maximum_bytes:
        raise TransportContractError("benchmark sandbox result must be bounded")
    required = {"schema", "invocation_id", "classification", "payload", "sandbox"}
    if (
        set(mapped) != required
        or mapped.get("schema") != "quant-runtime.sandbox-worker-result.v2"
        or mapped.get("invocation_id") != invocation_id
        or mapped.get("classification")
        not in {
            "success",
            "timeout",
            "cancellation",
            "policy_rejection",
            "resource_exhaustion",
            "strategy_rejection",
            "engine_failure",
        }
        or not isinstance(mapped.get("payload"), dict)
        or not isinstance(mapped.get("sandbox"), dict)
    ):
        raise TransportContractError("benchmark sandbox result is invalid")
    return mapped


def _blocked(request: dict[str, Any], code: str) -> dict[str, Any]:
    identity = {
        "schema": (
            "quant-runtime.benchmark-exec-result.v2"
            if request["schema"] == "quant-runtime.benchmark-exec-request.v2"
            else "quant-runtime.benchmark-exec-result.v1"
        ),
        "status": "blocked",
        "benchmark_invocation_id": request["invocation_id"],
        "sandbox_invocation_id": None,
        "classification": "policy_rejection",
        "payload": None,
        "sandbox": None,
        "error": {"code": code},
    }
    return {**identity, "execution_id": sha256_value(identity)}
