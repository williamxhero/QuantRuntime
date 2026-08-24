from __future__ import annotations

from datetime import date

import pytest

from quant_runtime.adapters.data.markethub import (
    AdapterStorage,
    MarketHubDataAdapter,
    SnapshotRequest,
)


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
