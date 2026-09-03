from __future__ import annotations

from typing import Any

from quant_runtime.sandbox.invocation import PreparedSandboxInvocation


class UnsupportedSandboxBackend:
    """Fail closed when no verified production containment backend is configured."""

    production = True

    def invoke(self, prepared: PreparedSandboxInvocation) -> dict[str, Any]:
        return {
            "schema": "quant-runtime.sandbox-worker-result.v1",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "policy_rejection",
            "payload": {"code": "sandbox_platform_unsupported"},
            "diagnostics": {
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "artifacts": 0,
                "truncated": False,
                "sanitized": True,
            },
        }
