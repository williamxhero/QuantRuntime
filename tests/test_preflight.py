from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

from conftest import PACKAGE, FixtureTransport
from strategy_workspace import WorkspaceClient, WorkspaceWorker
from test_executor_topologies import registry

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubDataAdapter
from quant_runtime.executor import RuntimeExecutor
from quant_runtime.preflight import RuntimePreflight


def draft(package_ref: dict) -> dict:
    return {
        "schema": "quant-research.runtime-preflight-request.v1",
        "strategy_package": package_ref,
        "snapshot_request": {
            "adapter": "markethub",
            "snapshot_mode": "reference",
            "trust_policy": "verified_immutable",
            "local_cache": "none",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
            "as_of": "2025-02-01T00:00:00Z",
            "required_semantics": ["field_availability", "time"],
            "query": {
                "instruments": ["SH.600000", "SZ.000001"],
                "start": "2025-01-01",
                "end": "2025-01-31",
                "frequency": "1d",
                "adjustment": "none",
                "calendar": "cn-equity-v1",
                "contract_mapping": None,
            },
        },
        "parameters": {},
        "execution": {
            "topology": "formal_only",
            "formal": [{"id": "primary", "adapter": "nautilus", "config": {}}],
        },
    }


def workspace_state(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def preflight(workspace: Path, market_fixture: dict) -> RuntimePreflight:
    return RuntimePreflight(
        WorkspaceClient(workspace),
        registry=registry(),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(market_fixture))
        ),
    )


def test_preflight_returns_a_stable_frozen_value_without_workspace_side_effects(
    tmp_path: Path, market_fixture: dict
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    before = workspace_state(workspace)

    first = preflight(workspace, market_fixture).preflight(draft(package["package_ref"]))
    second = preflight(workspace, market_fixture).preflight(draft(package["package_ref"]))

    assert first["status"] == "accepted"
    assert first["frozen_snapshot"]["schema"] == "quant-research.market-snapshot-ref.v2"
    assert first["frozen_snapshot"]["snapshot_id"] == second["frozen_snapshot"]["snapshot_id"]
    assert workspace_state(workspace) == before
    assert client.list_runs() == []
    assert client.list_records() == []


def test_versioned_preflight_exposes_runtime_owned_sample_observation(
    tmp_path: Path, market_fixture: dict
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    request = draft(package["package_ref"])
    request["schema"] = "quant-research.runtime-preflight-request.v4"

    result = preflight(workspace, market_fixture).preflight(request)

    assert result["schema"] == "quant-research.runtime-preflight-result.v2"
    assert result["status"] == "accepted"
    assert result["frozen_snapshot"]["schema"] == "quant-research.market-snapshot-ref.v2"
    assert result["observation"] == {
        "schema": "quant-runtime.data-change-observation.v1",
        "status": "evaluated",
        "as_of": "2025-02-01T00:00:00Z",
        "sample_count": 6,
        "instrument_sample_counts": [
            {"instrument": "SH.600000", "sample_count": 3},
            {"instrument": "SZ.000001", "sample_count": 3},
        ],
        "data_revision": "fixture-global-v1:fixture-daily-v1",
        "data_version": "fixture-global-v1",
        "dataset_version": "fixture-daily-v1",
        "catalog_hash": result["frozen_snapshot"]["verification"]["catalog_hash"],
        "calendar_hash": result["frozen_snapshot"]["verification"]["calendar_hash"],
        "coverage_hash": result["frozen_snapshot"]["verification"]["coverage_hash"],
        "reason": "Runtime observed exact canonical MarketHub samples",
    }


def test_preflight_failure_is_classified_and_leaves_workspace_unchanged(
    tmp_path: Path, market_fixture: dict
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    broken = deepcopy(market_fixture)
    broken["daily_pages"][0]["meta"]["complete"] = False
    before = workspace_state(workspace)

    result = preflight(workspace, broken).preflight(draft(package["package_ref"]))

    assert result["status"] == "failed"
    assert result["observation"]["classification"] == "market_data_incident"
    assert workspace_state(workspace) == before
    assert client.list_runs() == []
    assert client.list_records() == []


def test_required_unavailable_pit_semantics_fails_before_workspace_submission(
    tmp_path: Path, market_fixture: dict
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    value = draft(package["package_ref"])
    value["snapshot_request"]["required_semantics"] = ["point_in_time"]
    before = workspace_state(workspace)

    result = preflight(workspace, market_fixture).preflight(value)

    assert result["status"] == "failed"
    assert result["observation"]["code"] == "data_semantics_unavailable"
    assert workspace_state(workspace) == before
    assert client.list_runs() == []


def test_post_preflight_drift_fails_the_submitted_attempt_without_replacement_or_retry(
    tmp_path: Path, market_fixture: dict
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    prepared = preflight(workspace, market_fixture).preflight(draft(package["package_ref"]))
    assert prepared["status"] == "accepted"
    submitted = client.submit_run(
        {
            "schema": "quant-research.workspace-run-request.v3",
            "strategy_package": package["package_ref"],
            "market_snapshot": prepared["frozen_snapshot"],
            "parameters": {},
            "execution": draft(package["package_ref"])["execution"],
        }
    )

    class DriftTransport(FixtureTransport):
        def request_json(self, *args, **kwargs):
            value, size, elapsed = super().request_json(*args, **kwargs)
            if args[1] == "/api/health" and self.health_reads == 2:
                value["dataset_versions"]["stock_daily_1d"] = "fixture-daily-v2"
            return value, size, elapsed

    failed = RuntimeExecutor(
        client,
        WorkspaceWorker(workspace),
        registry=registry(),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=DriftTransport(market_fixture))
        ),
    ).execute(submitted["run_id"])

    assert failed["status"] == "failed"
    assert len(failed["attempts"]) == 1
    assert failed["request"]["market_snapshot"] == prepared["frozen_snapshot"]
    assert "version drift" in failed["error"]["message"]
