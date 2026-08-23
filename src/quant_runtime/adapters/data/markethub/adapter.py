from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from quant_runtime.contracts.canonical_hash import (
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_value,
    write_json,
)
from quant_runtime.market_data.markethub.catalog import CanonicalInstrument
from quant_runtime.market_data.markethub.client import MarketHubClient, MarketHubContractError
from quant_runtime.market_data.markethub.daily_data import CanonicalBar, CanonicalDataset
from quant_runtime.sdk.snapshot_contract import (
    SnapshotRequest,
    validate_snapshot_manifest,
)
from quant_runtime.workspace.atomic import AtomicDirectory
from quant_runtime.workspace.layout import RuntimeLayout

from .cache import MarketHubCache
from .publication import HttpPublicationSource, PublicationSource, PublishedPartition

ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    dataset: CanonicalDataset
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
    dataset: CanonicalDataset | None

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def mode(self) -> str:
        return str(self.manifest["mode"])


ClientFactory = Callable[[SnapshotRequest], MarketHubClient]


class MarketHubDataAdapter:
    name = "markethub"
    adapter_version = ADAPTER_VERSION

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        publication_source: PublicationSource | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda request: MarketHubClient(request.base_url))
        self._publication_source = publication_source

    def resolve(self, request: SnapshotRequest, layout: RuntimeLayout) -> ResolvedSnapshot:
        if request.snapshot_mode == "reference":
            return self._reference(request, layout)
        return self._materialized(request, layout)

    def cache(
        self,
        *,
        policy: str,
        snapshot: ResolvedSnapshot,
        layout: RuntimeLayout,
        consumer: str,
        run_id: str,
        evidence_root: Path,
    ):
        if snapshot.dataset is None:
            raise ValueError("cache conversion requires a loaded snapshot dataset")
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
        dataset = client.fetch_dataset(
            request.instruments,
            request.start,
            request.end,
        )
        catalog = tuple(item.hash_record() for item in dataset.instruments)
        calendar = tuple(item.isoformat() for item in dataset.trading_days)
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

    def _reference(self, request: SnapshotRequest, layout: RuntimeLayout) -> ResolvedSnapshot:
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

    def _materialized(self, request: SnapshotRequest, layout: RuntimeLayout) -> ResolvedSnapshot:
        verification = self.read(request)
        source = self._source(
            request,
            f"{verification.dataset.data_version}:{verification.dataset.dataset_version}",
        )
        reference_identity = {
            **request.identity_payload(),
            "source": source,
            "trust_policy": "verified_immutable",
        }
        reference_id = f"sha256:{sha256_value(reference_identity)}"
        publication = self._publication_source or HttpPublicationSource(request.base_url)
        declared = publication.list_partitions(
            request,
            market_data_version=verification.dataset.data_version,
            dataset_version=verification.dataset.dataset_version,
        )
        _validate_partition_catalog(declared, request)
        with AtomicDirectory(layout.staging) as staging:
            metadata = self._write_metadata(staging.path, verification)
            partition_records = []
            for item in sorted(declared, key=lambda value: (value.month, value.kind)):
                payload = publication.download(item)
                _verify_partition(item, payload)
                relative = Path(item.path)
                target = staging.path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                partition_records.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": item.sha256,
                        "content_bytes": item.content_bytes,
                        "month": item.month,
                        "kind": item.kind,
                        "rows": item.rows,
                    }
                )
            identity = {
                "source_snapshot_ref": reference_id,
                "partitions": partition_records,
                "catalog": metadata["catalog"],
                "calendar": metadata["calendar"],
                "coverage": metadata["coverage"],
            }
            local_dataset = _load_materialized_dataset(
                staging.path,
                source=source,
                query=request.identity_payload()["query"],
                catalog=metadata["catalog"],
                calendar=metadata["calendar"],
                partitions=partition_records,
            )
            identity["canonical_input_hash"] = local_dataset.input_hash
            snapshot_id = f"sha256:{sha256_value(identity)}"
            manifest = {
                "schema": "quant-research.market-snapshot.v1",
                "snapshot_id": snapshot_id,
                "mode": "materialized",
                "source_snapshot_ref": reference_id,
                "source": source,
                "query": request.identity_payload()["query"],
                "calendar": metadata["calendar"],
                "catalog": metadata["catalog"],
                "coverage": metadata["coverage"],
                "partitions": partition_records,
                "canonical_input_hash": local_dataset.input_hash,
                "resolved_at": _now(),
            }
            validate_snapshot_manifest(manifest)
            write_json(staging.path / "manifest.json", manifest)
            final = layout.snapshots / snapshot_id.removeprefix("sha256:")
            if final.exists():
                existing = read_json(final / "manifest.json")
                validate_snapshot_manifest(existing)
                if existing["snapshot_id"] != snapshot_id:
                    raise MarketHubContractError("snapshot identity collision")
                existing_dataset = _load_materialized_dataset(
                    final,
                    source=existing["source"],
                    query=existing["query"],
                    catalog=existing["catalog"],
                    calendar=existing["calendar"],
                    partitions=existing["partitions"],
                )
                return ResolvedSnapshot(
                    existing,
                    (final / "manifest.json").resolve(),
                    existing_dataset,
                )
            published = staging.publish(final)
            return ResolvedSnapshot(
                manifest,
                (published / "manifest.json").resolve(),
                local_dataset,
            )

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
        return f"{health.data_version}:{health.daily_dataset_version}"

    def _publish_manifest(
        self,
        layout: RuntimeLayout,
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

    @staticmethod
    def _write_metadata(
        root: Path,
        verification: SnapshotVerification,
    ) -> dict[str, dict[str, Any]]:
        result = {}
        for name, value in (
            ("catalog", verification.catalog),
            ("calendar", verification.calendar),
            ("coverage", verification.coverage),
        ):
            path = write_json(root / f"{name}.json", value)
            payload = path.read_bytes()
            result[name] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(payload),
                "content_bytes": len(payload),
            }
        return result


def _validate_partition_catalog(
    partitions: tuple[PublishedPartition, ...],
    request: SnapshotRequest,
) -> None:
    expected = _months(request)
    actual = tuple(
        (item.month, item.kind)
        for item in sorted(partitions, key=lambda item: (item.month, item.kind))
    )
    expected_files = tuple((month, kind) for month in expected for kind in ("bars", "coverage"))
    if actual != expected_files:
        raise MarketHubContractError(
            "published Parquet files do not cover requested bars and coverage: "
            f"expected={expected_files}, actual={actual}"
        )
    if any(
        item.content_bytes < 1
        or item.rows < 0
        or item.kind not in {"bars", "coverage"}
        or len(item.sha256) != 64
        or not item.download_url
        or item.path != f"year={item.month[:4]}/month={item.month[5:]}/{item.kind}.parquet"
        for item in partitions
    ):
        raise MarketHubContractError("published Parquet catalog has incomplete integrity fields")


def _verify_partition(partition: PublishedPartition, payload: bytes) -> None:
    if len(payload) != partition.content_bytes:
        raise MarketHubContractError(
            f"published Parquet byte count mismatch for {partition.month}: "
            f"{len(payload)} != {partition.content_bytes}"
        )
    digest = sha256_bytes(payload)
    if digest != partition.sha256:
        raise MarketHubContractError(
            f"published Parquet sha256 mismatch for {partition.month}: {digest}"
        )
    try:
        schema = pq.read_schema(pa.BufferReader(payload))
        metadata = pq.read_metadata(pa.BufferReader(payload))
    except Exception as exc:
        raise MarketHubContractError(
            f"published partition {partition.month} is not valid Parquet: {exc}"
        ) from exc
    required = (
        {
            "market",
            "code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "is_suspended",
            "is_st",
            "pre_close",
        }
        if partition.kind == "bars"
        else {"market", "code", "expected_rows", "actual_rows", "missing_rows", "complete"}
    )
    if not required <= set(schema.names):
        raise MarketHubContractError(
            f"published {partition.kind} schema lacks required columns: "
            f"{sorted(required - set(schema.names))}"
        )
    schema_version = (schema.metadata or {}).get(b"schema_version", b"").decode()
    if schema_version != "markethub-stock-daily-parquet-v1":
        raise MarketHubContractError(
            f"published {partition.kind} schema_version is invalid: {schema_version!r}"
        )
    if metadata.num_rows != partition.rows:
        raise MarketHubContractError(
            f"published {partition.kind} row count mismatch: "
            f"{metadata.num_rows} != {partition.rows}"
        )


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


def _months(request: SnapshotRequest) -> tuple[str, ...]:
    year, month = request.start.year, request.start.month
    end = request.end.year, request.end.month
    values = []
    while (year, month) <= end:
        values.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return tuple(values)


def _identity_without_time(value: dict[str, Any]) -> bytes:
    return canonical_json({key: item for key, item in value.items() if key != "resolved_at"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
