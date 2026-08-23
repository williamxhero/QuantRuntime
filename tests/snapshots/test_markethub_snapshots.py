from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import FixtureTransport

from quant_runtime.adapters.data.markethub import (
    MarketHubCache,
    MarketHubDataAdapter,
    PublishedPartition,
    ResolvedSnapshot,
)
from quant_runtime.adapters.data.markethub.publication import HttpPublicationSource
from quant_runtime.contracts.canonical_hash import sha256_bytes
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


def parquet_bytes(kind: str) -> bytes:
    sink = pa.BufferOutputStream()
    if kind == "bars":
        table = pa.table(
            {
                "market": ["SHSE", "SZSE"],
                "code": ["600000", "000001"],
                "trade_date": [date(2025, 1, 2), date(2025, 1, 2)],
                "open": [20.0, 10.0],
                "high": [20.2, 10.2],
                "low": [19.8, 9.8],
                "close": [20.1, 10.1],
                "volume": [1000.0, 1000.0],
                "amount": [20100.0, 10100.0],
                "is_suspended": [False, False],
                "is_st": [False, False],
                "pre_close": [20.0, 10.0],
            }
        )
    else:
        table = pa.table(
            {
                "market": ["SHSE", "SZSE"],
                "code": ["600000", "000001"],
                "expected_rows": [1, 1],
                "actual_rows": [1, 1],
                "missing_rows": [0, 0],
                "complete": [True, True],
            }
        )
    table = table.replace_schema_metadata({b"schema_version": b"markethub-stock-daily-parquet-v1"})
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


class PublicationFixture:
    def __init__(self, *, digest: str | None = None) -> None:
        self.payloads = {kind: parquet_bytes(kind) for kind in ("bars", "coverage")}
        self.partitions = tuple(
            PublishedPartition(
                month="2025-01",
                kind=kind,
                path=f"year=2025/month=01/{kind}.parquet",
                rows=2,
                content_bytes=len(self.payloads[kind]),
                sha256=(
                    digest if kind == "bars" and digest else sha256(self.payloads[kind]).hexdigest()
                ),
                download_url=f"fixture://2025-01/{kind}",
            )
            for kind in ("bars", "coverage")
        )

    def list_partitions(
        self,
        snapshot_request,
        *,
        market_data_version,
        dataset_version,
    ):
        assert market_data_version == "fixture-global-v1"
        assert dataset_version == "fixture-daily-v1"
        return self.partitions

    def download(self, partition):
        assert partition in self.partitions
        return self.payloads[partition.kind]


def test_reference_defaults_to_assumed_without_read_and_cache_is_not_identity(
    s_fixture,
    tmp_path: Path,
) -> None:
    transports = []

    def metadata_only_factory(_):
        transport = FixtureTransport(s_fixture)
        transports.append(transport)
        return MarketHubClient(transport=transport)

    adapter = MarketHubDataAdapter(client_factory=metadata_only_factory)
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    first = adapter.resolve(request(), layout)
    second = adapter.resolve(request(local_cache="persistent"), layout)
    assert first.snapshot_id == second.snapshot_id
    assert first.manifest_path == second.manifest_path
    assert first.manifest["trust_policy"] == "assumed_immutable"
    assert first.manifest["source"]["data_revision"] == ("fixture-global-v1:fixture-daily-v1")
    assert first.dataset is None
    assert "verification" not in first.manifest
    assert "local_cache" not in first.manifest
    assert len(transports) == 2
    assert all(item.health_reads == 1 and item.daily_page_index == 0 for item in transports)


def test_reference_read_fails_if_frozen_revision_drifts(s_fixture, tmp_path: Path) -> None:
    changed = json.loads(json.dumps(s_fixture))
    changed["health"]["dataset_versions"]["stock_daily_1d"] = "fixture-daily-v2"
    for page in changed["daily_pages"]:
        page["meta"]["dataset_version"] = "fixture-daily-v2"
    calls = iter((s_fixture, changed))
    adapter = MarketHubDataAdapter(
        client_factory=lambda _: MarketHubClient(transport=FixtureTransport(next(calls)))
    )
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    snapshot = adapter.resolve(request(), layout)
    with pytest.raises(MarketHubContractError, match="drifted before read"):
        adapter.read(
            request(),
            expected_revision=snapshot.manifest["source"]["data_revision"],
        )


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
    publication = PublicationFixture()
    adapter = MarketHubDataAdapter(
        client_factory=client_factory(s_fixture),
        publication_source=publication,
    )
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    snapshot = adapter.resolve(request(snapshot_mode="materialized"), layout)
    assert {item["kind"] for item in snapshot.manifest["partitions"]} == {
        "bars",
        "coverage",
    }
    for partition in snapshot.manifest["partitions"]:
        published = snapshot.manifest_path.parent / partition["path"]
        payload = publication.payloads[partition["kind"]]
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
        publication_source=PublicationFixture(digest="0" * 64),
    )
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    with pytest.raises(MarketHubContractError, match="sha256 mismatch"):
        adapter.resolve(request(snapshot_mode="materialized"), layout)
    assert list(layout.snapshots.iterdir()) == []
    assert list(layout.staging.iterdir()) == []


def test_http_publication_source_matches_real_export_manifest_contract() -> None:
    manifest_bytes = (
        Path(__file__).parents[1] / "fixtures" / "markethub_stock_daily_export_manifest.json"
    ).read_bytes()
    manifest = json.loads(manifest_bytes)
    mapping = {
        "dataset_id": "stock_daily_1d",
        "market_data_version": manifest["market_data_version"],
        "dataset_version": manifest["dataset_version"],
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "manifest_url": (f"/api/exports/stock_daily_1d/{manifest['dataset_version']}/manifest"),
    }

    def handle(http_request: httpx.Request) -> httpx.Response:
        if "/resolve/" in http_request.url.path:
            return httpx.Response(200, json=mapping)
        if http_request.url.path.endswith("/manifest"):
            return httpx.Response(200, content=manifest_bytes)
        raise AssertionError(http_request.url)

    source = HttpPublicationSource(
        "http://fixture",
        transport=httpx.MockTransport(handle),
    )
    files = source.list_partitions(
        request(),
        market_data_version=manifest["market_data_version"],
        dataset_version=manifest["dataset_version"],
    )
    assert [(item.kind, item.content_bytes) for item in files] == [
        ("bars", 3471835),
        ("coverage", 22234),
    ]


def test_none_ephemeral_and_persistent_cache_policies_are_observable(
    canonical_dataset,
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout.create(tmp_path / ".runtime")
    adapter = MarketHubDataAdapter()
    snapshot = ResolvedSnapshot(
        {"snapshot_id": "sha256:" + "a" * 64, "mode": "reference"},
        tmp_path / "manifest.json",
        canonical_dataset,
    )
    with adapter.cache(
        policy="none",
        snapshot=snapshot,
        layout=layout,
        consumer="nautilus",
        run_id="none-run",
        evidence_root=tmp_path / "none-evidence",
    ) as use:
        assert use.path is None
    assert not list(layout.cache.rglob("*.parquet"))
    none_manifest = json.loads(
        (tmp_path / "none-evidence/cache_conversion_manifest.json").read_text()
    )
    assert none_manifest["authoritative"] is False
    assert none_manifest["output"] is None

    with adapter.cache(
        policy="ephemeral",
        snapshot=snapshot,
        layout=layout,
        consumer="nautilus",
        run_id="ephemeral-run",
        evidence_root=tmp_path / "ephemeral-evidence",
    ) as use:
        ephemeral_path = use.path
        assert ephemeral_path is not None and ephemeral_path.is_dir()
        assert MarketHubCache.load(ephemeral_path).input_hash == canonical_dataset.input_hash
    assert ephemeral_path is not None and not ephemeral_path.exists()
    ephemeral_manifest = json.loads(
        (tmp_path / "ephemeral-evidence/cache_conversion_manifest.json").read_text()
    )
    assert ephemeral_manifest["retained"] is False
    assert len(ephemeral_manifest["output"]["sha256"]) == 64

    paths = []
    for index in range(2):
        with adapter.cache(
            policy="persistent",
            snapshot=snapshot,
            layout=layout,
            consumer="nautilus",
            run_id=f"persistent-run-{index}",
            evidence_root=tmp_path / f"persistent-evidence-{index}",
        ) as use:
            assert use.path is not None and use.path.is_dir()
            assert MarketHubCache.load(use.path).input_hash == canonical_dataset.input_hash
            paths.append(use.path)
    assert paths[0] == paths[1]
    persistent_manifest = json.loads(
        (tmp_path / "persistent-evidence-1/cache_conversion_manifest.json").read_text()
    )
    assert persistent_manifest["authoritative"] is False
    assert persistent_manifest["retained"] is True
    assert persistent_manifest["reused"] is True
