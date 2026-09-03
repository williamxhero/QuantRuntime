from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from strategy_workspace import WorkspaceClient

from quant_runtime.adapters.data.markethub import (
    AdapterStorage,
    MarketHubDataAdapter,
    SnapshotRequest,
)
from quant_runtime.preflight import RuntimePreflight


@pytest.mark.connected
def test_live_markethub_reference_snapshot_is_stable(tmp_path) -> None:
    """Probe the production data source without making live data a structure gate."""
    request = SnapshotRequest(
        adapter="markethub",
        snapshot_mode="reference",
        trust_policy="verified_immutable",
        local_cache="none",
        endpoint_contract="v2",
        base_url="http://yosef-server:8803",
        instruments=("SH.600000", "SZ.000001"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 6),
        frequency="1d",
        adjustment="none",
        calendar="cn-equity-v1",
        contract_mapping=None,
    )
    storage = AdapterStorage.create(tmp_path / "markethub")
    adapter = MarketHubDataAdapter()

    frozen = adapter.resolve(request, storage)
    opened = adapter.open_snapshot(frozen.manifest, storage)

    assert frozen.mode == "reference"
    assert opened.snapshot_id == frozen.snapshot_id
    assert opened.dataset is not None
    assert len(opened.dataset.bars) == 6


@pytest.mark.connected
def test_live_preflight_returns_a_frozen_reference_without_submitting_a_run(tmp_path) -> None:
    workspace = WorkspaceClient(tmp_path / "workspace")
    package = workspace.register_package(Path(__file__).parent / "fixtures" / "noop-strategy")

    result = RuntimePreflight(workspace).preflight(
        {
            "schema": "quant-research.runtime-preflight-request.v1",
            "strategy_package": package["package_ref"],
            "snapshot_request": {
                "adapter": "markethub",
                "snapshot_mode": "reference",
                "trust_policy": "verified_immutable",
                "local_cache": "none",
                "endpoint_contract": "v2",
                "base_url": "http://yosef-server:8803",
                "as_of": "2025-01-06T16:00:00Z",
                "required_semantics": ["field_availability", "time"],
                "query": {
                    "instruments": ["SH.600000", "SZ.000001"],
                    "start": "2025-01-02",
                    "end": "2025-01-06",
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
    )

    assert result["status"] == "accepted", result
    assert result["frozen_snapshot"]["source"]["data_revision"]
    assert workspace.list_runs() == []
    assert workspace.list_records() == []


@pytest.mark.connected
def test_live_markethub_futures_1m_accepts_nullable_open_interest(tmp_path) -> None:
    request = SnapshotRequest(
        adapter="markethub",
        snapshot_mode="reference",
        trust_policy="verified_immutable",
        local_cache="none",
        endpoint_contract="v2",
        base_url="http://yosef-server:8803",
        instruments=("agL0",),
        start=date(2025, 1, 2),
        end=date(2025, 1, 2),
        frequency="1m",
        adjustment="back_adjusted",
        calendar="cn-futures-v1",
        contract_mapping="back_adjusted_continuous",
    )
    storage = AdapterStorage.create(tmp_path / "markethub-futures")
    frozen = MarketHubDataAdapter().resolve(request, storage)
    opened = MarketHubDataAdapter().open_snapshot(frozen.manifest, storage)

    assert opened.dataset is not None
    assert opened.dataset.bars
    assert any(item.open_interest is None for item in opened.dataset.bars)
    assert all(item.adjustment_offset is not None for item in opened.dataset.bars)
