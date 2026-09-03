from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class PreparedSandboxInvocation:
    protocol: dict[str, Any]
    package: StrategyPackage
    inputs: Path
    output: Path


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
        input_refs: Mapping[str, str],
    ) -> dict[str, Any]:
        if phase not in {"behavioral_conformance", "discovery", "formal"}:
            raise SandboxInvocationError("sandbox requested phase is invalid")
        frozen_refs = {str(key): str(value) for key, value in sorted(input_refs.items())}
        if not frozen_refs or any(
            not value.startswith("sha256:") or len(value) != 71 for value in frozen_refs.values()
        ):
            raise SandboxInvocationError("sandbox input references are invalid")
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
            identity = {
                "schema": "quant-runtime.sandbox-invocation.v1",
                "package": package.package_ref,
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
            protocol = {**identity, "invocation_id": "sha256:" + sha256_value(identity)}
            prepared = PreparedSandboxInvocation(protocol, package, inputs, output)
            return _worker_result(self._backend.invoke(prepared), protocol["invocation_id"])


def _worker_result(value: Mapping[str, Any], invocation_id: str) -> dict[str, Any]:
    result = {str(key): item for key, item in value.items()}
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
