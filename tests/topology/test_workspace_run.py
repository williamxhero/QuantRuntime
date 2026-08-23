from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from conftest import FixtureTransport

from quant_runtime.adapters.data.markethub import MarketHubDataAdapter
from quant_runtime.adapters.formal.nautilus import NautilusWorkspaceAdapter
from quant_runtime.market_data.markethub.client import MarketHubClient
from quant_runtime.sdk.capability_contract import AdapterRegistry, CapabilityProfile
from quant_runtime.workspace import StrategyWorkspace
from quant_runtime.workspace.registry import production_registry

ROOT = Path(__file__).parents[2]


def request(*, discovery: bool) -> dict:
    return {
        "schema": "quant-research.workspace-run-request.v1",
        "package": str((ROOT / "strategies" / "equity" / "cross-sectional-momentum").resolve()),
        "parameters": {
            "lookback_days": 3,
            "top_k": 1,
            "rebalance_frequency": "daily",
            "signal_timing": "close",
            "execution_timing": "next_open",
            "price_adjustment": "none",
        },
        "data": {
            "adapter": "markethub",
            "snapshot_mode": "reference",
            "trust_policy": "assumed_immutable",
            "local_cache": "none",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
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
        "discovery": {"mode": "run", "backend": "qlib"} if discovery else {"mode": "skip"},
        "formal": {"mode": "pinned", "backend": "nautilus", "config": {}},
    }


def workspace(s_fixture, tmp_path: Path) -> StrategyWorkspace:
    return StrategyWorkspace(
        tmp_path / ".runtime",
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(s_fixture))
        ),
    )


def test_formal_only_run_skips_discovery_and_keeps_one_engine_state_owner(
    s_fixture,
    tmp_path: Path,
) -> None:
    manifest, path = workspace(s_fixture, tmp_path).run(request(discovery=False))
    assert manifest["status"] == "completed"
    assert manifest["topology"] == {
        "discovery": None,
        "formal_mode": "pinned",
        "formal_backends": ["nautilus"],
    }
    assert manifest["discovery_artifact_hash"] is None
    assert manifest["formal_runs"][0]["backend_id"] == "nautilus"
    assert path.is_file()


def test_discovery_artifact_is_lineage_only_and_formal_runs_independently(
    s_fixture,
    tmp_path: Path,
) -> None:
    manifest, path = workspace(s_fixture, tmp_path).run(request(discovery=True))
    assert manifest["status"] == "completed"
    assert manifest["topology"]["discovery"] == "qlib"
    assert len(manifest["discovery_artifact_hash"]) == 64
    assert (path.parent / "discovery" / "qlib" / "discovery_manifest.json").is_file()
    assert (path.parent / "formal" / "nautilus" / "normalized_output.json").is_file()


def test_non_none_cache_is_actually_consumed_by_formal_adapter(s_fixture, tmp_path: Path) -> None:
    value = request(discovery=False)
    value["data"]["local_cache"] = "persistent"
    manifest, path = workspace(s_fixture, tmp_path).run(value)
    result = json.loads((path.parent / manifest["result"]["relative_path"]).read_text())
    metrics = result["formal_results"][0]["metrics"]
    assert metrics["cache_consumed"] is True
    assert metrics["read_method"] == "non_authoritative_cache"
    assert list((tmp_path / ".runtime" / "cache" / "persistent").rglob("bars.parquet"))


def test_run_identity_binds_request_cache_and_capability_versions(
    s_fixture,
    tmp_path: Path,
) -> None:
    base = request(discovery=True)
    first, _ = workspace(s_fixture, tmp_path / "first").run(base)
    repeated, _ = workspace(s_fixture, tmp_path / "repeated").run(deepcopy(base))
    assert first["run_id"] == repeated["run_id"]

    changed_discovery = deepcopy(base)
    changed_discovery["discovery"]["config"] = {"feature_set": "alternate"}
    discovery_manifest, _ = workspace(s_fixture, tmp_path / "discovery").run(changed_discovery)
    assert discovery_manifest["run_id"] != first["run_id"]

    changed_cache = deepcopy(base)
    changed_cache["data"]["local_cache"] = "ephemeral"
    cache_manifest, _ = workspace(s_fixture, tmp_path / "cache").run(changed_cache)
    assert cache_manifest["run_id"] != first["run_id"]

    formal_only = request(discovery=False)
    baseline, _ = workspace(s_fixture, tmp_path / "baseline").run(formal_only)
    engine_manifest, _ = StrategyWorkspace(
        tmp_path / "engine" / ".runtime",
        registry=_versioned_registry(engine_version="test-engine-upgrade"),
        data_adapter=_fixture_adapter(s_fixture),
    ).run(formal_only)
    assert engine_manifest["run_id"] != baseline["run_id"]

    class VersionedDataAdapter(MarketHubDataAdapter):
        adapter_version = "test-data-adapter-upgrade"

    data_manifest, _ = StrategyWorkspace(
        tmp_path / "data-adapter" / ".runtime",
        data_adapter=VersionedDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(s_fixture))
        ),
    ).run(formal_only)
    assert data_manifest["run_id"] != baseline["run_id"]


def _fixture_adapter(s_fixture) -> MarketHubDataAdapter:
    return MarketHubDataAdapter(
        client_factory=lambda _: MarketHubClient(transport=FixtureTransport(s_fixture))
    )


def _versioned_registry(*, engine_version: str) -> AdapterRegistry:
    original = production_registry().profile("formal", "nautilus")
    registry = AdapterRegistry()
    registry.register(
        CapabilityProfile.from_dict(
            {
                "schema": "quant-research.runtime-capability.v1",
                "backend_id": original.backend_id,
                "role": original.role,
                "adapter_version": original.adapter_version,
                "engine_version": engine_version,
                "provides": sorted(original.provides),
                "conditional": {
                    name: {"policy": policy} for name, policy in original.conditional.items()
                },
            }
        ),
        NautilusWorkspaceAdapter,
    )
    return registry
