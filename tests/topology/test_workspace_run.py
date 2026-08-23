from __future__ import annotations

from pathlib import Path

from conftest import FixtureTransport

from quant_runtime.adapters.data.markethub import MarketHubDataAdapter
from quant_runtime.market_data.markethub.client import MarketHubClient
from quant_runtime.workspace import StrategyWorkspace

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
