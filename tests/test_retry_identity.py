from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from strategy_workspace import WorkspaceClient, WorkspaceError

from quant_runtime.adapters.data.markethub import ResolvedSnapshot
from quant_runtime.executor import sandbox_input_identity, sandbox_input_publication
from quant_runtime.package import StrategyPackage


def _inputs(tmp_path: Path) -> dict[str, Any]:
    package = StrategyPackage(
        tmp_path,
        {"strategy_id": "fixture", "revision": 1, "package_hash": "a" * 64},
        {
            "schema": "quant-research.strategy-package.v2",
            "strategy_id": "fixture",
            "revision": 1,
            "requirements": {"capabilities": []},
        },
    )
    profile = {
        "schema": "quant-runtime.sandbox-profile.v1",
        "profile_id": "s596-calibrated-oci",
        "revision": 1,
        "execution_mode": "isolated",
        "trust_classification": "generated_untrusted",
        "containment": {
            "backend_id": "docker-engine-linux-oci",
            "proof": "sha256:" + "b" * 64,
        },
        "dependency_environment": {
            "kind": "oci-image",
            "identity": "sha256:" + "c" * 64,
        },
        "capabilities": {"filesystem": "sealed", "network": "deny", "subprocess": "bounded"},
        "limits": {
            "cpu_seconds": 60,
            "memory_bytes": 2**31,
            "wall_clock_seconds": 120,
            "processes": 127,
            "filesystem_bytes": 10 * 1024 * 1024,
            "stdout_bytes": 65536,
            "stderr_bytes": 65536,
            "artifacts": 32,
        },
    }
    snapshot = ResolvedSnapshot(
        {
            "schema": "quant-research.market-snapshot-ref.v2",
            "snapshot_id": "sha256:" + "d" * 64,
            "resolved_at": "2026-10-03T12:00:10Z",
        },
        tmp_path / "manifest.json",
        None,
    )
    return {
        "request_schema": "quant-research.workspace-run-request.v5",
        "request_hash": "e" * 64,
        "package": package,
        "profile": profile,
        "conformance": {
            "schema": "quant-runtime.behavioral-conformance-ref.v1",
            "conformance_id": "sha256:" + "f" * 64,
            "artifact": {"sha256": "1" * 64},
        },
        "parameters": {"ema_fast_period": 10},
        "snapshot": snapshot,
        "capsule_id": "sha256:" + "2" * 64,
        "phase": "formal",
        "phase_id": "primary",
        "config": {"market_data": {"local_cache": "none"}},
    }


def _record_id(values: dict[str, Any]) -> str:
    identity = sandbox_input_identity(**values)
    return sandbox_input_publication(identity)["record_id"]


def test_exact_request_replay_is_idempotent(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    first = sandbox_input_publication(sandbox_input_identity(**values))
    replay = sandbox_input_publication(sandbox_input_identity(**deepcopy(values)))

    assert replay == first
    assert first["record_id"].startswith("sandbox-input-v2.")


def test_changed_request_gets_new_workspace_run_id(tmp_path: Path) -> None:
    client = WorkspaceClient(tmp_path / "workspace")
    package = client.register_package(Path(__file__).parent / "fixtures" / "noop-strategy")
    request = {
        "schema": "quant-research.workspace-run-request.v2",
        "strategy_package": package["package_ref"],
        "market_snapshot": {
            "schema": "quant-research.market-snapshot-ref.v1",
            "snapshot_id": "sha256:" + "a" * 64,
            "mode": "reference",
            "trust_policy": "assumed_immutable",
            "source": {
                "adapter": "markethub",
                "adapter_version": "1.0.0",
                "endpoint_contract": "v2",
                "base_url": "http://fixture",
                "data_revision": "fixture-global-v1:fixture-daily-v1",
            },
            "query": {
                "instruments": ["SH.600000"],
                "start": "2025-01-01",
                "end": "2025-01-31",
                "frequency": "1d",
                "adjustment": "none",
            },
            "calendar": "cn-equity-v1",
            "contract_mapping": None,
            "resolved_at": "2026-10-03T12:00:10Z",
        },
        "parameters": {"fixture_mode": "formal"},
        "execution": {
            "topology": "formal_only",
            "formal": [{"id": "primary", "adapter": "nautilus", "config": {}}],
        },
    }
    first = client.submit_run(request)
    changed = deepcopy(request)
    changed["market_snapshot"]["resolved_at"] = "2026-10-03T12:01:10Z"
    second = client.submit_run(changed)

    assert second["run_id"] != first["run_id"]
    assert second["request_hash"] != first["request_hash"]


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(request_schema="quant-research.workspace-run-request.v4"),
        lambda value: value.update(request_hash="3" * 64),
        lambda value: value["package"].package_ref.update(package_hash="4" * 64),
        lambda value: value["package"].manifest.update(revision=2),
        lambda value: value["profile"]["limits"].update(memory_bytes=2**31 + 1),
        lambda value: value["profile"]["dependency_environment"].update(
            identity="sha256:" + "5" * 64
        ),
        lambda value: value["parameters"].update(ema_fast_period=11),
        lambda value: value["snapshot"].manifest.update(snapshot_id="sha256:" + "6" * 64),
        lambda value: value.update(capsule_id="sha256:" + "7" * 64),
        lambda value: value["conformance"]["artifact"].update(sha256="8" * 64),
        lambda value: value["snapshot"].manifest.update(resolved_at="2026-10-03T12:01:10Z"),
    ],
    ids=[
        "request-version",
        "request-content",
        "package-ref",
        "package-manifest",
        "profile",
        "oci-image",
        "parameters",
        "data-snapshot",
        "data-capsule",
        "oci-conformance-receipt",
        "resolved-at",
    ],
)
def test_changed_retry_content_gets_new_sandbox_record_id(
    tmp_path: Path,
    change,
) -> None:
    original = _inputs(tmp_path)
    changed = deepcopy(original)
    change(changed)

    assert _record_id(changed) != _record_id(original)


def test_same_record_id_with_different_content_fails_closed_before_submit(tmp_path: Path) -> None:
    client = WorkspaceClient(tmp_path / "workspace")
    publication = sandbox_input_publication(sandbox_input_identity(**_inputs(tmp_path)))
    published = client.publish_record(publication)
    assert published["payload"] == publication["payload"]
    assert client.publish_record(publication) == published

    changed = deepcopy(publication)
    changed["payload"]["resolved_at"] = "2026-10-03T12:01:10Z"
    with pytest.raises(WorkspaceError, match="different content"):
        client.publish_record(changed)
