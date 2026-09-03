from __future__ import annotations

from typing import Any

from quant_runtime.sandbox.invocation import PreparedSandboxInvocation
from quant_runtime.sandbox.outcome import bounded_diagnostics, sandbox_outcome


class UnsupportedSandboxBackend:
    """Fail closed when no verified production containment backend is configured."""

    production = True

    def invoke(self, prepared: PreparedSandboxInvocation) -> dict[str, Any]:
        profile = prepared.protocol.get("sandbox_profile", {})
        limits = profile.get("limits", {}) if isinstance(profile, dict) else {}
        diagnostics = bounded_diagnostics(
            limits={
                "stdout_bytes": int(limits.get("stdout_bytes", 0)),
                "stderr_bytes": int(limits.get("stderr_bytes", 0)),
                "artifacts": int(limits.get("artifacts", 0)),
            },
            stdout_bytes=0,
            stderr_bytes=0,
            artifact_count=0,
            artifact_bytes=0,
            artifacts_accepted=0,
            terminal_proof={
                "backend_id": "unsupported",
                "mechanism_version": "unsupported",
                "proof_id": "sha256:" + "0" * 64,
                "candidate_terminated": True,
                "descendants_terminated": True,
                "running_processes": 0,
            },
        )
        payload = {"code": "sandbox_platform_unsupported"}
        outcome = sandbox_outcome("policy_rejection", diagnostics=diagnostics, payload=payload)
        return {
            "schema": "quant-runtime.sandbox-worker-result.v2",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "policy_rejection",
            "payload": payload,
            "sandbox": outcome,
        }
