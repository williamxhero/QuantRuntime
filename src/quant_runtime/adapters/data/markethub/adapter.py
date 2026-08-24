from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from quant_runtime.adapters.data.markethub.catalog import CanonicalInstrument
from quant_runtime.adapters.data.markethub.client import MarketHubClient, MarketHubContractError
from quant_runtime.adapters.data.markethub.futures_model import CanonicalFuturesDataset
from quant_runtime.adapters.data.markethub.model import CanonicalBar, CanonicalDataset
from quant_runtime.artifacts import (
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_value,
    write_json,
)
from quant_runtime.atomic import AtomicDirectory

from .cache import CacheUse, MarketHubCache
from .contract import SnapshotRequest, validate_snapshot_manifest
from .storage import AdapterStorage

ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    dataset: CanonicalDataset | CanonicalFuturesDataset
    catalog: tuple[dict[str, Any], ...]
    calendar: tuple[str, ...]
    coverage: tuple[dict[str, Any], ...]

    @property
    def manifest_value(self) -> dict[str, str]:
        return {
            "canonical_input_hash": self.dataset.input_hash,
            "data_version": self.dataset.data_version,
            "dataset_version": self.dataset.dataset_version,
            "catalog_hash": sha256_value(self.catalog),
            "calendar_hash": sha256_value(self.calendar),
            "coverage_hash": sha256_value(self.coverage),
        }


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    manifest: dict[str, Any]
    manifest_path: Path
    dataset: CanonicalDataset | CanonicalFuturesDataset | None

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def mode(self) -> str:
        return str(self.manifest["mode"])


ClientFactory = Callable[[SnapshotRequest], MarketHubClient]
ArtifactMaterializer = Callable[[str, Path], Path]


class MarketHubDataAdapter:
    name = "markethub"
    adapter_version = ADAPTER_VERSION

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda request: MarketHubClient(request.base_url))

    def resolve(self, request: SnapshotRequest, layout: AdapterStorage) -> ResolvedSnapshot:
        if request.snapshot_mode == "reference":
            return self._reference(request, layout)
        raise MarketHubContractError(
            "materialized snapshots must be published as Strategy Workspace ArtifactRefs"
        )

    def open_snapshot(
        self,
        manifest: dict[str, Any],
        layout: AdapterStorage,
        *,
        materialize_artifact: ArtifactMaterializer | None = None,
    ) -> ResolvedSnapshot:
        """Open a frozen Workspace snapshot and verify its bytes or MarketHub revision."""
        validate_snapshot_manifest(manifest)
        target = layout.snapshots / str(manifest["snapshot_id"]).removeprefix("sha256:")
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = write_json(target / "manifest.json", manifest).resolve()
        if manifest["mode"] == "reference":
            request = SnapshotRequest.from_manifest(manifest)
            expected_revision = manifest["source"].get("data_revision")
            if not isinstance(expected_revision, str) or not expected_revision:
                raise MarketHubContractError("reference snapshot lacks a frozen data revision")
            verification = self.read(request, expected_revision=expected_revision)
            declared = manifest.get("verification")
            if declared is not None and declared != verification.manifest_value:
                raise MarketHubContractError("reference snapshot verification drifted")
            return ResolvedSnapshot(manifest, manifest_path, verification.dataset)

        if materialize_artifact is None:
            raise MarketHubContractError(
                "materialized snapshot requires Workspace artifact materialization"
            )
        if manifest.get("query", {}).get("frequency") == "1m":
            raise MarketHubContractError(
                "materialized futures snapshots require a versioned futures partition contract; "
                "use a frozen MarketHub reference snapshot"
            )
        local_metadata = {
            name: _materialize_ref(
                manifest[name],
                target / "metadata" / f"{name}.json",
                target,
                materialize_artifact,
            )
            for name in ("catalog", "calendar", "coverage")
        }
        local_partitions = []
        for item in manifest["partitions"]:
            if not isinstance(item, dict) or not isinstance(item.get("artifact"), dict):
                raise MarketHubContractError("materialized partition lacks an artifact reference")
            local_partitions.append(
                {
                    **_materialize_ref(
                        item["artifact"],
                        target / "partitions" / str(item["month"]) / f"{item['kind']}.parquet",
                        target,
                        materialize_artifact,
                    ),
                    "month": item["month"],
                    "kind": item["kind"],
                    "rows": item["rows"],
                }
            )
        dataset = _load_materialized_dataset(
            target,
            source=manifest["source"],
            query=manifest["query"],
            catalog=local_metadata["catalog"],
            calendar=local_metadata["calendar"],
            partitions=local_partitions,
        )
        if dataset.input_hash != manifest["canonical_input_hash"]:
            raise MarketHubContractError("materialized snapshot canonical input hash mismatch")
        return ResolvedSnapshot(manifest, manifest_path, dataset)

    def cache(
        self,
        *,
        policy: str,
        snapshot: ResolvedSnapshot,
        layout: AdapterStorage,
        consumer: str,
        run_id: str,
        evidence_root: Path,
    ):
        if snapshot.dataset is None:
            raise ValueError("cache conversion requires a loaded snapshot dataset")
        if isinstance(snapshot.dataset, CanonicalFuturesDataset):
            if policy != "none":
                raise ValueError("futures snapshots currently require market_data.local_cache=none")
            return _no_cache(evidence_root, snapshot, consumer)
        return MarketHubCache(layout).prepare(
            policy=policy,
            snapshot_id=snapshot.snapshot_id,
            dataset=snapshot.dataset,
            consumer=consumer,
            run_id=run_id,
            evidence_root=evidence_root,
        )

    def read(
        self,
        request: SnapshotRequest,
        *,
        expected_revision: str | None = None,
    ) -> SnapshotVerification:
        client = self._client_factory(request)
        if request.frequency == "1m":
            dataset = client.fetch_futures_dataset(
                request.instruments,
                request.start,
                request.end,
                series_type=str(request.contract_mapping),
            )
        else:
            dataset = client.fetch_dataset(
                request.instruments,
                request.start,
                request.end,
            )
        catalog = tuple(item.hash_record() for item in dataset.instruments)
        calendar = (
            tuple(item.isoformat() for item in dataset.trading_days)
            if isinstance(dataset, CanonicalDataset)
            else tuple(sorted({item.bar_time.date().isoformat() for item in dataset.bars}))
        )
        bar_counts = {instrument: 0 for instrument in request.instruments}
        for bar in dataset.bars:
            bar_counts[bar.instrument] += 1
        coverage = tuple(
            {
                "instrument": instrument,
                "actual_rows": bar_counts[instrument],
                "complete": bar_counts[instrument] > 0,
            }
            for instrument in request.instruments
        )
        if any(not item["complete"] for item in coverage):
            raise MarketHubContractError(f"snapshot coverage is incomplete: {coverage!r}")
        actual_revision = f"{dataset.data_version}:{dataset.dataset_version}"
        if expected_revision is not None and actual_revision != expected_revision:
            raise MarketHubContractError(
                "MarketHub reference snapshot drifted before read: "
                f"{expected_revision!r} -> {actual_revision!r}"
            )
        return SnapshotVerification(dataset, catalog, calendar, coverage)

    def _reference(self, request: SnapshotRequest, layout: AdapterStorage) -> ResolvedSnapshot:
        verification = None
        if request.trust_policy == "verified_immutable":
            verification = self.read(request)
        revision = (
            f"{verification.dataset.data_version}:{verification.dataset.dataset_version}"
            if verification is not None
            else self._resolve_revision(request)
        )
        source = self._source(request, revision)
        identity = {
            **request.identity_payload(),
            "source": source,
            "trust_policy": request.trust_policy,
        }
        snapshot_id = f"sha256:{sha256_value(identity)}"
        manifest = {
            "schema": "quant-research.market-snapshot-ref.v1",
            "snapshot_id": snapshot_id,
            "mode": "reference",
            "trust_policy": (
                "verified_immutable" if verification is not None else request.trust_policy
            ),
            "source": source,
            "query": identity["query"],
            "calendar": request.calendar,
            "contract_mapping": request.contract_mapping,
            "resolved_at": _now(),
        }
        if verification is not None:
            manifest["verification"] = verification.manifest_value
        validate_snapshot_manifest(manifest)
        path = self._publish_manifest(layout, snapshot_id, manifest)
        return ResolvedSnapshot(manifest, path, verification.dataset if verification else None)

    def _source(
        self,
        request: SnapshotRequest,
        revision: str,
    ) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "adapter_version": self.adapter_version,
            "endpoint_contract": request.endpoint_contract,
            "base_url": request.base_url,
            "data_revision": revision,
        }

    def _resolve_revision(self, request: SnapshotRequest) -> str:
        health = self._client_factory(request).open()
        dataset_version = (
            health.futures_1m_dataset_version
            if request.frequency == "1m"
            else health.daily_dataset_version
        )
        if not dataset_version:
            raise MarketHubContractError(
                f"MarketHub health lacks the {request.frequency} dataset version"
            )
        return f"{health.data_version}:{dataset_version}"

    def _publish_manifest(
        self,
        layout: AdapterStorage,
        snapshot_id: str,
        manifest: dict[str, Any],
    ) -> Path:
        final = layout.snapshots / snapshot_id.removeprefix("sha256:")
        path = final / "manifest.json"
        if path.exists():
            existing = read_json(path)
            validate_snapshot_manifest(existing)
            if _identity_without_time(existing) != _identity_without_time(manifest):
                raise MarketHubContractError("snapshot identity collision")
            return path.resolve()
        with AtomicDirectory(layout.staging, final) as staging:
            write_json(staging.path / "manifest.json", manifest)
            return (staging.publish() / "manifest.json").resolve()


def _load_materialized_dataset(
    root: Path,
    *,
    source: dict[str, Any],
    query: dict[str, Any],
    catalog: dict[str, Any],
    calendar: dict[str, Any],
    partitions: list[dict[str, Any]],
) -> CanonicalDataset:
    catalog_rows = _read_verified_json_array(root, catalog)
    calendar_rows = _read_verified_json_array(root, calendar)
    instruments = tuple(_instrument_from_record(item) for item in catalog_rows)
    instrument_by_code = {item.raw_code: item for item in instruments}
    requested_codes = set(instrument_by_code)
    start = date.fromisoformat(str(query["start"]))
    end = date.fromisoformat(str(query["end"]))
    bar_rows: list[dict[str, Any]] = []
    coverage_keys: set[tuple[str, str]] = set()
    for record in partitions:
        path = _verified_path(root, record)
        table = pq.read_table(path)
        if len(table) != int(record["rows"]):
            raise MarketHubContractError(f"materialized row count changed: {record['path']}")
        if record["kind"] == "coverage":
            for row in table.to_pylist():
                code = str(row.get("code", ""))
                if code not in requested_codes:
                    continue
                if (
                    row.get("complete") is not True
                    or int(row.get("missing_rows", -1)) != 0
                    or int(row.get("expected_rows", -1)) != int(row.get("actual_rows", -2))
                ):
                    raise MarketHubContractError(
                        f"materialized coverage is incomplete: {record['month']}/{code}"
                    )
                coverage_keys.add((str(record["month"]), code))
            continue
        for row in table.to_pylist():
            code = str(row.get("code", ""))
            trading_day = row.get("trade_date")
            if code not in requested_codes or not isinstance(trading_day, date):
                continue
            if start <= trading_day <= end:
                bar_rows.append({**row, "trade_time": trading_day.isoformat()})
    expected_coverage = {
        (month, code) for month in _months_from_query(query) for code in requested_codes
    }
    if coverage_keys != expected_coverage:
        raise MarketHubContractError(
            "materialized coverage does not contain every requested instrument-month"
        )
    try:
        bars = tuple(
            sorted(
                (CanonicalBar.from_markethub(row, instrument_by_code) for row in bar_rows),
                key=lambda item: item.identity,
            )
        )
    except (KeyError, ValueError) as exc:
        raise MarketHubContractError(f"invalid materialized daily bars: {exc}") from exc
    revision = str(source.get("data_revision", ""))
    data_version, separator, dataset_version = revision.partition(":")
    if not separator:
        raise MarketHubContractError("materialized snapshot source revision is invalid")
    dataset = CanonicalDataset(
        data_version=data_version,
        dataset_version=dataset_version,
        timezone="Asia/Shanghai",
        instruments=instruments,
        trading_days=tuple(date.fromisoformat(str(item)) for item in calendar_rows),
        bars=bars,
    )
    try:
        dataset.validate()
    except ValueError as exc:
        raise MarketHubContractError(f"materialized dataset validation failed: {exc}") from exc
    return dataset


def _read_verified_json_array(root: Path, record: dict[str, Any]) -> list[Any]:
    path = _verified_path(root, record)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketHubContractError(f"materialized metadata is invalid: {path}") from exc
    if not isinstance(value, list):
        raise MarketHubContractError(f"materialized metadata must be an array: {path}")
    return value


def _verified_path(root: Path, record: dict[str, Any]) -> Path:
    path = (root / str(record["path"])).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise MarketHubContractError(
            f"materialized file escapes snapshot: {record['path']}"
        ) from exc
    payload = path.read_bytes()
    if len(payload) != int(record["content_bytes"]) or sha256_bytes(payload) != record["sha256"]:
        raise MarketHubContractError(f"materialized file integrity mismatch: {record['path']}")
    return path


def _instrument_from_record(value: Any) -> CanonicalInstrument:
    if not isinstance(value, dict):
        raise MarketHubContractError("materialized catalog item must be an object")
    return CanonicalInstrument(
        instrument=str(value["instrument"]),
        raw_code=str(value["raw_code"]),
        exchange=str(value["exchange"]),
        currency=str(value["currency"]),
        price_precision=int(value["price_precision"]),
        tick_size=Decimal(str(value["tick_size"])),
        lot_size=int(value["lot_size"]),
        list_date=date.fromisoformat(value["list_date"]) if value.get("list_date") else None,
        delist_date=(
            date.fromisoformat(value["delist_date"]) if value.get("delist_date") else None
        ),
        is_st=bool(value.get("is_st", False)),
    )


def _months_from_query(query: dict[str, Any]) -> tuple[str, ...]:
    start = date.fromisoformat(str(query["start"]))
    end = date.fromisoformat(str(query["end"]))
    year, month = start.year, start.month
    result = []
    while (year, month) <= (end.year, end.month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return tuple(result)


def _materialize_ref(
    artifact: dict[str, Any],
    destination: Path,
    root: Path,
    materialize: ArtifactMaterializer,
) -> dict[str, Any]:
    required = {"uri", "sha256", "bytes"}
    if missing := required - artifact.keys():
        raise MarketHubContractError(f"materialized artifact ref lacks fields: {sorted(missing)}")
    path = materialize(str(artifact["uri"]), destination)
    payload = path.read_bytes()
    if sha256_bytes(payload) != artifact["sha256"]:
        raise MarketHubContractError(f"materialized artifact hash mismatch: {artifact['uri']}")
    if len(payload) != int(artifact["bytes"]):
        raise MarketHubContractError(f"materialized artifact byte mismatch: {artifact['uri']}")
    return {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": artifact["sha256"],
        "content_bytes": artifact["bytes"],
    }


def _identity_without_time(value: dict[str, Any]) -> bytes:
    return canonical_json({key: item for key, item in value.items() if key != "resolved_at"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def _no_cache(evidence_root: Path, snapshot: ResolvedSnapshot, consumer: str):
    evidence_path = write_json(
        evidence_root / "cache_conversion_manifest.json",
        {
            "schema": "quant-research.cache-conversion.v1",
            "policy": "none",
            "authoritative": False,
            "source_snapshot_id": snapshot.snapshot_id,
            "source_input_hash": snapshot.dataset.input_hash if snapshot.dataset else None,
            "consumer": consumer,
            "transform_version": None,
            "output": None,
            "retained": False,
        },
    )
    yield CacheUse("none", None, None, evidence_path)
