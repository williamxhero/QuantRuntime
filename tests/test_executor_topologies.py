from __future__ import annotations

from pathlib import Path

import pytest
from conftest import PACKAGE, FixtureTransport
from strategy_workspace import WorkspaceClient, WorkspaceWorker

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubDataAdapter
from quant_runtime.adapters.interface import DiscoveryAdapterResult, FormalAdapterResult
from quant_runtime.capabilities import AdapterRegistry, CapabilityProfile
from quant_runtime.executor import RuntimeExecutor


class FixtureDiscoveryAdapter:
    name = "qlib"

    def run(self, value):
        value.output.mkdir(parents=True, exist_ok=True)
        path = value.output / "qlib_native.json"
        path.write_text('{"native":true}', encoding="utf-8")
        return DiscoveryAdapterResult("qlib", "test", "test", "d" * 64, {"rows": 1}, ())


class FixtureFormalAdapter:
    name = "nautilus"

    def run(self, value, *, formal_id: str):
        if value.config.get("raise"):
            raise RuntimeError("fixture formal failure")
        value.output.mkdir(parents=True, exist_ok=True)
        (value.output / "native_orders.csv").write_text("order_id\n", encoding="utf-8")
        return FormalAdapterResult(
            formal_id=formal_id,
            backend_id="nautilus",
            adapter_version="test-adapter",
            engine_version="test-engine",
            status="completed",
            metrics={"score": float(value.config.get("score", 1.0)), "native": True},
            positions=(),
            fills=(),
            account_curve=(),
            native_evidence=(),
        )


def registry() -> AdapterRegistry:
    value = AdapterRegistry()
    for role, name, factory in (
        ("discovery", "qlib", FixtureDiscoveryAdapter),
        ("formal", "nautilus", FixtureFormalAdapter),
    ):
        value.register(
            CapabilityProfile.from_dict(
                {
                    "backend_id": name,
                    "role": role,
                    "adapter_version": "test-adapter",
                    "engine_version": "test-engine",
                    "provides": ["data.bar.1d"],
                }
            ),
            factory,
        )
    return value


def snapshot() -> dict:
    return {
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
            "instruments": ["SH.600000", "SZ.000001"],
            "start": "2025-01-01",
            "end": "2025-01-31",
            "frequency": "1d",
            "adjustment": "none",
        },
        "calendar": "cn-equity-v1",
        "contract_mapping": None,
        "resolved_at": "2026-08-24T00:00:00Z",
    }


def request(package_ref: dict, topology: str) -> dict:
    formal = [{"id": "primary", "adapter": "nautilus", "config": {"score": 1.0}}]
    execution = {"topology": topology, "formal": formal}
    if topology == "discovery_formal":
        execution["discovery"] = {"adapter": "qlib", "config": {}}
    if topology in {"formal_comparison", "agreement_gate"}:
        execution["formal"].append(
            {"id": "challenger", "adapter": "nautilus", "config": {"score": 2.0}}
        )
    if topology == "agreement_gate":
        execution["agreement"] = {
            "selectors": ["score"],
            "absolute_tolerance": 0.0,
            "relative_tolerance": 0.0,
            "require_all": True,
        }
    return {
        "schema": "quant-research.workspace-run-request.v2",
        "strategy_package": package_ref,
        "market_snapshot": snapshot(),
        "parameters": {},
        "execution": execution,
    }


def executor(workspace: Path, market_fixture: dict) -> RuntimeExecutor:
    return RuntimeExecutor(
        WorkspaceClient(workspace),
        WorkspaceWorker(workspace),
        registry=registry(),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(market_fixture))
        ),
    )


@pytest.mark.parametrize(
    ("topology", "outcome"),
    [
        ("formal_only", "completed"),
        ("discovery_formal", "completed"),
        ("formal_comparison", "completed"),
        ("agreement_gate", "rejected"),
    ],
)
def test_runtime_executor_supports_all_topologies(
    topology: str,
    outcome: str,
    tmp_path: Path,
    market_fixture: dict,
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(request(package["package_ref"], topology))
    completed = executor(workspace, market_fixture).execute(submitted["run_id"])
    assert completed["status"] == outcome
    assert completed["result"]["outcome"] == outcome
    assert set(completed["result"]["formal"]) == (
        {"primary"}
        if topology in {"formal_only", "discovery_formal"}
        else {"primary", "challenger"}
    )
    if topology == "discovery_formal":
        assert completed["result"]["discovery"]["adapter"] == "qlib"
    if topology == "agreement_gate":
        assert completed["result"]["comparison"]["gate"]["passed"] is False
    assert any(
        item["logical_role"] == "runtime-manifest" for item in completed["result"]["artifacts"]
    )


def test_same_request_is_idempotent_and_failed_retry_creates_new_attempt(
    tmp_path: Path,
    market_fixture: dict,
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    value = request(package["package_ref"], "formal_only")
    value["execution"]["formal"][0]["config"] = {"raise": True}
    first = client.submit_run(value)
    failed = executor(workspace, market_fixture).execute(first["run_id"])
    assert failed["status"] == "failed"
    same = client.submit_run(value)
    assert same["run_id"] == first["run_id"]
    same_execution = executor(workspace, market_fixture).execute(first["run_id"])
    assert same_execution["attempts"] == failed["attempts"]
    retried = client.retry_run(first["run_id"])
    assert len(retried["attempts"]) == 2
    failed_again = executor(workspace, market_fixture).execute(first["run_id"])
    assert failed_again["status"] == "failed"
    assert failed_again["attempts"][1]["attempt_id"] != failed_again["attempts"][0]["attempt_id"]


def test_run_identity_binds_request_snapshot_topology_and_engine_versions(
    tmp_path: Path,
    market_fixture: dict,
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(request(package["package_ref"], "formal_only"))
    completed = executor(workspace, market_fixture).execute(submitted["run_id"])
    identity = completed["attempts"][0]["runtime_identity"]
    assert identity["request_id"] == submitted["run_id"]
    assert identity["strategy_package"] == package["package_ref"]
    assert identity["snapshot_id"] == snapshot()["snapshot_id"]
    assert identity["topology"] == "formal_only"
    assert identity["formal"][0]["adapter"]["engine_version"] == "test-engine"
    assert identity["formal"][0]["read_semantics"] == {
        "local_cache": "none",
        "method": "snapshot_native",
    }
