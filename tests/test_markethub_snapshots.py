from __future__ import annotations

import io
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import FixtureTransport
from strategy_workspace import WorkspaceClient
from test_executor_topologies import snapshot

from quant_runtime.adapters.data.markethub import (
    AdapterStorage,
    CanonicalBar,
    CanonicalDataset,
    MarketHubClient,
    MarketHubContractError,
    MarketHubDataAdapter,
    SnapshotRequest,
)
from quant_runtime.adapters.data.markethub.catalog import CanonicalInstrument


def test_reference_snapshot_detects_version_drift_and_never_persists_raw_bars(
    tmp_path: Path,
    market_fixture: dict,
) -> None:
    class DriftTransport(FixtureTransport):
        def request_json(self, *args, **kwargs):
            value, size, elapsed = super().request_json(*args, **kwargs)
            if args[1] == "/api/health" and self.health_reads == 2:
                value["dataset_versions"]["stock_daily_1d"] = "fixture-daily-v2"
            return value, size, elapsed

    adapter = MarketHubDataAdapter(
        client_factory=lambda _: MarketHubClient(transport=DriftTransport(market_fixture))
    )
    storage = AdapterStorage.create(tmp_path / "adapter")
    with pytest.raises(MarketHubContractError, match="version drift"):
        adapter.open_snapshot(snapshot(), storage)
    assert not list(storage.root.rglob("*.parquet"))
    assert not list(storage.root.rglob("*.csv"))


def test_reference_rejects_ordering_duplicates_and_incomplete_delivery(
    market_fixture: dict,
) -> None:
    for mutate, match in (
        (lambda value: value["daily_pages"][0]["items"].reverse(), "ordering violation"),
        (
            lambda value: value["daily_pages"][0]["items"].__setitem__(
                1, deepcopy(value["daily_pages"][0]["items"][0])
            ),
            "ordering violation",
        ),
        (lambda value: value["daily_pages"][0]["meta"].update(complete=False), "complete"),
    ):
        broken = deepcopy(market_fixture)
        mutate(broken)
        client = MarketHubClient(transport=FixtureTransport(broken))
        with pytest.raises(MarketHubContractError, match=match):
            client.fetch_dataset(
                ("SH.600000", "SZ.000001"),
                date(2025, 1, 1),
                date(2025, 1, 31),
                page_size=4,
            )


def test_materialized_snapshot_consumes_only_workspace_artifact_refs(tmp_path: Path) -> None:
    workspace = WorkspaceClient(tmp_path / "workspace")
    instrument = CanonicalInstrument(
        instrument="SZ.000001",
        raw_code="000001",
        exchange="SZSE",
        currency="CNY",
        price_precision=2,
        tick_size=Decimal("0.01"),
        lot_size=100,
        list_date=date(1991, 4, 3),
        delist_date=None,
    )
    bar = CanonicalBar(
        trading_day=date(2025, 1, 2),
        instrument=instrument.instrument,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
        pre_close=Decimal("10"),
        is_suspended=False,
        is_st=False,
    )
    dataset = CanonicalDataset(
        data_version="fixture-global-v1",
        dataset_version="fixture-daily-v1",
        timezone="Asia/Shanghai",
        instruments=(instrument,),
        trading_days=(date(2025, 1, 2),),
        bars=(bar,),
    )
    refs = {
        "catalog": _publish(
            workspace,
            "catalog.json",
            json.dumps([instrument.hash_record()]).encode(),
        ),
        "calendar": _publish(workspace, "calendar.json", b'["2025-01-02"]'),
        "coverage": _publish(workspace, "coverage.json", b"[]"),
        "bars": _publish(workspace, "bars.parquet", _parquet("bars")),
        "partition_coverage": _publish(
            workspace, "partition-coverage.parquet", _parquet("coverage")
        ),
    }
    manifest = {
        "schema": "quant-research.market-snapshot.v1",
        "snapshot_id": "sha256:" + "b" * 64,
        "mode": "materialized",
        "source_snapshot_ref": "sha256:" + "a" * 64,
        "source": {
            "adapter": "markethub",
            "adapter_version": "1.0.0",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
            "data_revision": "fixture-global-v1:fixture-daily-v1",
        },
        "query": {
            "instruments": ["SZ.000001"],
            "start": "2025-01-01",
            "end": "2025-01-31",
            "frequency": "1d",
            "adjustment": "none",
        },
        "calendar": refs["calendar"],
        "catalog": refs["catalog"],
        "coverage": refs["coverage"],
        "partitions": [
            {"artifact": refs["bars"], "month": "2025-01", "kind": "bars", "rows": 1},
            {
                "artifact": refs["partition_coverage"],
                "month": "2025-01",
                "kind": "coverage",
                "rows": 1,
            },
        ],
        "canonical_input_hash": dataset.input_hash,
        "resolved_at": "2026-08-24T00:00:00Z",
    }
    opened = MarketHubDataAdapter().open_snapshot(
        manifest,
        AdapterStorage.create(tmp_path / "adapter"),
        materialize_artifact=lambda uri, destination: Path(
            workspace.materialize_artifact(uri, destination)["path"]
        ),
    )
    assert opened.dataset is not None
    assert opened.dataset.input_hash == dataset.input_hash


def test_runtime_refuses_to_publish_path_based_materialized_snapshots(tmp_path: Path) -> None:
    request = SnapshotRequest(
        adapter="markethub",
        snapshot_mode="materialized",
        trust_policy="verified_immutable",
        local_cache="none",
        endpoint_contract="v2",
        base_url="http://fixture",
        instruments=("SZ.000001",),
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        frequency="1d",
        adjustment="none",
        calendar="cn-equity-v1",
        contract_mapping=None,
    )
    with pytest.raises(MarketHubContractError, match="Strategy Workspace ArtifactRefs"):
        MarketHubDataAdapter().resolve(request, AdapterStorage.create(tmp_path / "adapter"))


def _publish(client: WorkspaceClient, name: str, payload: bytes) -> dict:
    record = client.publish_record(
        {"record_id": f"record-{name}", "record_type": "test", "payload": {}},
        artifacts=[{"source": payload, "name": name, "logical_role": "snapshot-input"}],
    )
    return record["artifacts"][0]


def _parquet(kind: str) -> bytes:
    if kind == "bars":
        table = pa.Table.from_pylist(
            [
                {
                    "code": "000001",
                    "trade_date": date(2025, 1, 2),
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "volume": "1000",
                    "amount": "10500",
                    "pre_close": "10",
                    "is_suspended": False,
                    "is_st": False,
                }
            ]
        )
    else:
        table = pa.Table.from_pylist(
            [
                {
                    "code": "000001",
                    "complete": True,
                    "missing_rows": 0,
                    "expected_rows": 1,
                    "actual_rows": 1,
                }
            ]
        )
    output = io.BytesIO()
    pq.write_table(table, output)
    return output.getvalue()
