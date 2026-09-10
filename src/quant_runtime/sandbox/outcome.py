from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_runtime.artifacts import sha256_value

CLASSIFICATIONS = frozenset(
    {
        "success",
        "timeout",
        "cancellation",
        "policy_rejection",
        "resource_exhaustion",
        "strategy_rejection",
        "engine_failure",
    }
)
RETRYABLE = {
    "success": False,
    "timeout": True,
    "cancellation": True,
    "policy_rejection": False,
    "resource_exhaustion": False,
    "strategy_rejection": False,
    "engine_failure": True,
}
TERMINAL_STATUS = {
    "success": "completed",
    "strategy_rejection": "rejected",
    "timeout": "failed",
    "cancellation": "failed",
    "policy_rejection": "failed",
    "resource_exhaustion": "failed",
    "engine_failure": "failed",
}
ERROR_CODE = {
    "timeout": "sandbox_timeout",
    "cancellation": "sandbox_cancelled",
    "policy_rejection": "sandbox_policy_rejected",
    "resource_exhaustion": "sandbox_resource_exhausted",
    "engine_failure": "sandbox_engine_failed",
}


def sandbox_outcome(
    classification: str,
    *,
    diagnostics: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if classification not in CLASSIFICATIONS:
        raise ValueError("sandbox outcome classification is invalid")
    value = {
        "schema": "quant-runtime.sandbox-outcome.v1",
        "classification": classification,
        "retryable": RETRYABLE[classification],
        "terminal_status": TERMINAL_STATUS[classification],
        "diagnostics": dict(diagnostics),
    }
    identity = {
        **value,
        "payload_hash": sha256_value(dict(payload)),
    }
    return {**value, "evidence_hash": sha256_value(identity)}


def workspace_run_error(outcome: Mapping[str, Any]) -> dict[str, Any]:
    classification = str(outcome.get("classification", ""))
    if classification not in ERROR_CODE:
        raise ValueError("sandbox outcome does not map to a Workspace failure")
    return {
        "schema": "quant-research.run-error.v2",
        "code": ERROR_CODE[classification],
        "message": ERROR_CODE[classification].replace("_", " "),
        "retryable": RETRYABLE[classification],
        "details": {},
        "sandbox": dict(outcome),
    }


def bounded_diagnostics(
    *,
    limits: Mapping[str, int],
    stdout_bytes: int,
    stderr_bytes: int,
    artifact_count: int,
    artifact_bytes: int,
    artifacts_accepted: int,
    terminal_proof: Mapping[str, Any],
) -> dict[str, Any]:
    stdout_limit = int(limits["stdout_bytes"])
    stderr_limit = int(limits["stderr_bytes"])
    artifact_limit = int(limits["artifacts"])
    stdout_retained = min(max(0, stdout_bytes), stdout_limit)
    stderr_retained = min(max(0, stderr_bytes), stderr_limit)
    accepted = min(max(0, artifacts_accepted), artifact_limit)
    stdout_truncated = stdout_bytes > stdout_retained
    stderr_truncated = stderr_bytes > stderr_retained
    artifact_truncated = artifact_count > accepted
    return {
        "schema": "quant-runtime.sandbox-diagnostics.v1",
        "stdout": {
            "observed_bytes": max(0, stdout_bytes),
            "retained_bytes": stdout_retained,
            "limit_bytes": stdout_limit,
            "truncated": stdout_truncated,
        },
        "stderr": {
            "observed_bytes": max(0, stderr_bytes),
            "retained_bytes": stderr_retained,
            "limit_bytes": stderr_limit,
            "truncated": stderr_truncated,
        },
        "artifacts": {
            "observed": max(0, artifact_count),
            "accepted": accepted,
            "dropped": max(0, artifact_count - accepted),
            "bytes": max(0, artifact_bytes),
        },
        "truncated": stdout_truncated or stderr_truncated or artifact_truncated,
        "sanitized": True,
        "terminal_proof": dict(terminal_proof),
    }
