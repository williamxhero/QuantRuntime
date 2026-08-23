from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import FixtureTransport

from quant_runtime.adapters.data.markethub import MarketHubDataAdapter, PublishedPartition
from quant_runtime.market_data.markethub.client import MarketHubClient, MarketHubContractError
from quant_runtime.sdk.snapshot_contract import SnapshotRequest
from quant_runtime.workspace.layout import RuntimeLayout


def request(**changes) -> SnapshotRequest:
    value = SnapshotRequest.from_dict(
        {
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
        }
    )
    return replace(value, **changes)


def client_factory(s_fixture):
    return lambda _: MarketHubClient(transport=FixtureTransport(s_fixture))


def parquet_bytes() -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(
        pa.table({"trade_time": ["2025-01-02"], "code": ["600000"], "close": [10.0]}),
        sink,
    )
    return sink.getvalue().to_pybytes()


class PublicationFixture:
    def __init__(self, payload: bytes, *, digest: str | None = None) -> None:
        self.payload = payload
        self.partition = PublishedPartition(
            month="2025-01",
            path="published/2025-01.parquet",
            content_bytes=len(payload),
            sha256=digest or sha256(payload).hexdigest(),
            download_url="fixture://2025-01",
        )

    def list_partitions(self, snapshot_request):
        return (self.partition,)

    def download(self, partition):
        assert partition == self.partition
        return self.payload


def test_reference_defaults_to_assumed_without_read_and_cache_is_not_identity(
    tmp_path: Path,
) -> None:
    adapter = MarketHubDataAdapter(client_factory=lambda _: pytest.fail("unexpected read"))
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    first = adapter.resolve(request(), layout)
    second = adapter.resolve(request(local_cache="persistent"), layout)
    assert first.snapshot_id == second.snapshot_id
    assert first.manifest_path == second.manifest_path
    assert first.manifest["trust_policy"] == "assumed_immutable"
    assert first.dataset is None
    assert "verification" not in first.manifest
    assert "local_cache" not in first.manifest


def test_verified_reference_is_only_emitted_after_real_read(s_fixture, tmp_path: Path) -> None:
    adapter = MarketHubDataAdapter(client_factory=client_factory(s_fixture))
    snapshot = adapter.resolve(
        request(trust_policy="verified_immutable"),
        RuntimeLayout.create(tmp_path / ".runtime"),
    )
    assert snapshot.dataset is not None
    assert snapshot.manifest["trust_policy"] == "verified_immutable"
    assert snapshot.manifest["verification"]["canonical_input_hash"] == snapshot.dataset.input_hash


def test_materialized_preserves_and_verifies_published_parquet_bytes(
    s_fixture,
    tmp_path: Path,
) -> None:
    payload = parquet_bytes()
    adapter = MarketHubDataAdapter(
        client_factory=client_factory(s_fixture),
        publication_source=PublicationFixture(payload),
    )
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    snapshot = adapter.resolve(request(snapshot_mode="materialized"), layout)
    partition = snapshot.manifest["partitions"][0]
    published = snapshot.manifest_path.parent / partition["path"]
    assert published.read_bytes() == payload
    assert partition["content_bytes"] == len(payload)
    assert partition["sha256"] == sha256(payload).hexdigest()
    assert all(name in snapshot.manifest for name in ("catalog", "calendar", "coverage"))
    assert list(layout.staging.iterdir()) == []


def test_materialized_hash_mismatch_fails_closed_and_cleans_staging(
    s_fixture,
    tmp_path: Path,
) -> None:
    adapter = MarketHubDataAdapter(
        client_factory=client_factory(s_fixture),
        publication_source=PublicationFixture(parquet_bytes(), digest="0" * 64),
    )
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    with pytest.raises(MarketHubContractError, match="sha256 mismatch"):
        adapter.resolve(request(snapshot_mode="materialized"), layout)
    assert list(layout.snapshots.iterdir()) == []
    assert list(layout.staging.iterdir()) == []
