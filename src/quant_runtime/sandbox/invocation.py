from __future__ import annotations

import os
import re
import shutil
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from quant_runtime.artifacts import sha256_value
from quant_runtime.materialization import (
    PackageMaterializationError,
    VerifiedPackageMaterializer,
    WorkspacePackageArtifactPort,
)
from quant_runtime.package import StrategyPackage
from quant_runtime.sandbox.policy import SandboxPolicyRegistry


class SandboxInvocationError(ValueError):
    """A sandbox invocation or worker result violates its public protocol."""


class CancellationToken:
    """Thread-safe caller cancellation observed by the production backend."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class PreparedSandboxInvocation:
    protocol: dict[str, Any]
    package: StrategyPackage
    inputs: Path
    output: Path
    cancellation: CancellationToken


class SandboxBackend(Protocol):
    production: bool

    def invoke(self, prepared: PreparedSandboxInvocation) -> Mapping[str, Any]: ...


class SandboxRunner:
    """Prepare one sealed invocation and delegate it to an isolation backend."""

    def __init__(
        self,
        client: WorkspacePackageArtifactPort,
        *,
        backend: SandboxBackend,
        policy_registry: SandboxPolicyRegistry | None = None,
    ) -> None:
        self._client = client
        self._materializer = VerifiedPackageMaterializer(client)
        self._backend = backend
        self._policy_registry = policy_registry or SandboxPolicyRegistry()

    def invoke(
        self,
        *,
        package_record: Mapping[str, Any],
        profile: Mapping[str, Any],
        phase: str,
        parameters: Mapping[str, Any],
        input_artifacts: Mapping[str, Mapping[str, Any]],
        cancellation: CancellationToken | None = None,
        phase_config: Mapping[str, Any] | None = None,
        output_destination: Path | None = None,
    ) -> dict[str, Any]:
        if phase not in {"behavioral_conformance", "discovery", "formal"}:
            raise SandboxInvocationError("sandbox requested phase is invalid")
        artifacts = _input_artifacts(input_artifacts)
        resolved = self._policy_registry.resolve(package_record, profile)
        with TemporaryDirectory(prefix="quant-runtime-sandbox-") as temporary:
            root = Path(temporary)
            try:
                package = self._materializer.materialize(package_record, root / "package")
            except PackageMaterializationError as exc:
                raise SandboxInvocationError(str(exc)) from None
            inputs = root / "inputs"
            output = root / "output"
            inputs.mkdir()
            output.mkdir()
            frozen_refs = {
                name: _materialize_input(self._client, artifact, inputs / name)
                for name, artifact in artifacts.items()
            }
            identity = {
                "schema": "quant-runtime.sandbox-invocation.v1",
                "package": package.package_ref,
                "sandbox_profile": resolved.profile,
                "profile_hash": resolved.identity_hash,
                "dependency_environment": resolved.profile["dependency_environment"],
                "phase": phase,
                "parameters_hash": sha256_value(dict(parameters)),
                "input_refs": frozen_refs,
                "mounts": {
                    "package": "/sandbox/package",
                    "inputs": "/sandbox/inputs",
                    "output": "/sandbox/output",
                },
            }
            if phase in {"behavioral_conformance", "formal"}:
                identity["package_manifest"] = package.manifest
            if phase_config:
                identity["phase_config"] = dict(phase_config)
            protocol = {**identity, "invocation_id": "sha256:" + sha256_value(identity)}
            prepared = PreparedSandboxInvocation(
                protocol, package, inputs, output, cancellation or CancellationToken()
            )
            result = _worker_result(self._backend.invoke(prepared), protocol["invocation_id"])
            if output_destination is not None and result["classification"] == "success":
                _copy_worker_output(output / "staging", output_destination)
            return result


def _input_artifacts(
    value: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not value:
        raise SandboxInvocationError("sandbox input artifacts are required")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_artifact in sorted(value.items()):
        name = str(raw_name)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise SandboxInvocationError("sandbox input logical name is invalid")
        artifact = {str(key): item for key, item in raw_artifact.items()}
        required = {"uri", "sha256", "bytes"}
        if not required <= artifact.keys():
            raise SandboxInvocationError("sandbox input artifact reference is incomplete")
        digest = str(artifact["sha256"])
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or artifact["uri"] != "workspace-artifact://sha256/" + digest
            or not isinstance(artifact["bytes"], int)
            or isinstance(artifact["bytes"], bool)
            or artifact["bytes"] < 0
        ):
            raise SandboxInvocationError("sandbox input artifact identity is invalid")
        result[name] = artifact
    return result


def _materialize_input(client: Any, artifact: dict[str, Any], destination: Path) -> str:
    verification = client.verify_artifact(str(artifact["uri"]))
    if verification.get("verified") is not True:
        raise SandboxInvocationError("sandbox input artifact verification failed")
    verified = verification.get("artifact")
    if not isinstance(verified, Mapping) or any(
        verified.get(key) != artifact[key] for key in ("uri", "sha256", "bytes")
    ):
        raise SandboxInvocationError("sandbox input artifact verification identity mismatch")
    materialized = client.materialize_artifact(str(artifact["uri"]), destination)
    expected = destination.resolve()
    if materialized.get("materialized") is not True:
        raise SandboxInvocationError("sandbox input artifact materialization failed")
    try:
        actual = Path(str(materialized["path"])).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise SandboxInvocationError("sandbox input artifact materialization is invalid") from exc
    if actual != expected:
        raise SandboxInvocationError("sandbox input artifact materialized outside its sealed path")
    metadata = os.lstat(actual)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or actual.is_symlink()
        or (getattr(metadata, "st_file_attributes", 0) & 0x400)
    ):
        raise SandboxInvocationError("sandbox input artifact is not a regular sealed file")
    payload = actual.read_bytes()
    if len(payload) != artifact["bytes"] or sha256(payload).hexdigest() != artifact["sha256"]:
        raise SandboxInvocationError("sandbox input artifact bytes changed after verification")
    return "sha256:" + str(artifact["sha256"])


def _worker_result(value: Mapping[str, Any], invocation_id: str) -> dict[str, Any]:
    result = {str(key): item for key, item in value.items()}
    if result.get("schema") == "quant-runtime.sandbox-worker-result.v2":
        if set(result) != {"schema", "invocation_id", "classification", "payload", "sandbox"}:
            raise SandboxInvocationError("sandbox worker v2 result shape is invalid")
        if result["invocation_id"] != invocation_id:
            raise SandboxInvocationError("sandbox worker result identity mismatch")
        sandbox = result.get("sandbox")
        if not isinstance(result.get("payload"), Mapping) or not isinstance(sandbox, Mapping):
            raise SandboxInvocationError("sandbox worker v2 payload is invalid")
        if sandbox.get("classification") != result.get("classification"):
            raise SandboxInvocationError("sandbox worker outcome classification mismatch")
        return {**result, "payload": dict(result["payload"]), "sandbox": dict(sandbox)}
    if (
        set(result)
        != {
            "schema",
            "invocation_id",
            "classification",
            "payload",
            "diagnostics",
        }
        or result.get("schema") != "quant-runtime.sandbox-worker-result.v1"
    ):
        raise SandboxInvocationError("sandbox worker result shape is invalid")
    if result["invocation_id"] != invocation_id:
        raise SandboxInvocationError("sandbox worker result identity mismatch")
    if result["classification"] not in {
        "success",
        "timeout",
        "cancellation",
        "policy_rejection",
        "resource_exhaustion",
        "strategy_rejection",
        "engine_failure",
    }:
        raise SandboxInvocationError("sandbox worker classification is invalid")
    if not isinstance(result["payload"], Mapping) or not isinstance(result["diagnostics"], Mapping):
        raise SandboxInvocationError("sandbox worker payload and diagnostics must be objects")
    diagnostics = dict(result["diagnostics"])
    if set(diagnostics) != {
        "stdout_bytes",
        "stderr_bytes",
        "artifacts",
        "truncated",
        "sanitized",
    }:
        raise SandboxInvocationError("sandbox worker diagnostics shape is invalid")
    return {**result, "payload": dict(result["payload"]), "diagnostics": diagnostics}


def _copy_worker_output(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SandboxInvocationError("successful sandbox invocation lacks output artifacts")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.as_posix() == "sandbox-result.json" or path.name.startswith("."):
            continue
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise SandboxInvocationError("sandbox output contains an unsupported file type")
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
