from __future__ import annotations

from copy import deepcopy

import pytest

from quant_runtime.sandbox.outcome import (
    ERROR_CODE,
    RETRYABLE,
    TERMINAL_STATUS,
    bounded_diagnostics,
    sandbox_outcome,
    workspace_run_error,
)


def diagnostics() -> dict:
    return bounded_diagnostics(
        limits={"stdout_bytes": 8, "stderr_bytes": 4, "artifacts": 2},
        stdout_bytes=12,
        stderr_bytes=3,
        artifact_count=4,
        artifact_bytes=100,
        artifacts_accepted=2,
        terminal_proof={
            "backend_id": "docker-engine-linux-oci",
            "mechanism_version": "fixture",
            "proof_id": "sha256:" + "a" * 64,
            "candidate_terminated": True,
            "descendants_terminated": True,
            "running_processes": 0,
        },
    )


@pytest.mark.parametrize("classification", sorted(RETRYABLE))
def test_outcomes_are_mutually_exclusive_and_deterministically_mapped(
    classification: str,
) -> None:
    outcome = sandbox_outcome(classification, diagnostics=diagnostics(), payload={"code": "x"})

    assert outcome["classification"] == classification
    assert outcome["retryable"] is RETRYABLE[classification]
    assert outcome["terminal_status"] == TERMINAL_STATUS[classification]
    assert len(outcome["evidence_hash"]) == 64
    assert outcome["diagnostics"]["stdout"] == {
        "observed_bytes": 12,
        "retained_bytes": 8,
        "limit_bytes": 8,
        "truncated": True,
    }
    assert outcome["diagnostics"]["artifacts"]["dropped"] == 2
    if classification in ERROR_CODE:
        error = workspace_run_error(outcome)
        assert error["code"] == ERROR_CODE[classification]
        assert error["retryable"] is RETRYABLE[classification]
        assert error["sandbox"] == outcome
    else:
        with pytest.raises(ValueError, match="does not map"):
            workspace_run_error(outcome)


def test_diagnostic_counts_and_terminal_proof_change_evidence_identity() -> None:
    first = sandbox_outcome("success", diagnostics=diagnostics(), payload={})
    changed = deepcopy(diagnostics())
    changed["stdout"]["observed_bytes"] += 1
    second = sandbox_outcome("success", diagnostics=changed, payload={})
    terminal = deepcopy(diagnostics())
    terminal["terminal_proof"]["proof_id"] = "sha256:" + "b" * 64
    third = sandbox_outcome("success", diagnostics=terminal, payload={})

    assert len({first["evidence_hash"], second["evidence_hash"], third["evidence_hash"]}) == 3
