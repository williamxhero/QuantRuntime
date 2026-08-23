from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
from quant_runtime.market_data.markethub.client import MarketHubClient, MarketHubContractError
from quant_runtime.market_data.markethub.daily_data import CanonicalDataset
from quant_runtime.sdk.snapshot_contract import (
    SnapshotRequest,
    validate_snapshot_manifest,
)
from quant_runtime.workspace.atomic import AtomicDirectory
from quant_runtime.workspace.layout import RuntimeLayout

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

    def read(self, request: SnapshotRequest) -> SnapshotVerification:
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
        return SnapshotVerification(dataset, catalog, calendar, coverage)

    def _reference(self, request: SnapshotRequest, layout: RuntimeLayout) -> ResolvedSnapshot:
        verification = None
        if request.trust_policy == "verified_immutable":
            verification = self.read(request)
        source = self._source(request, verification)
        identity = {**request.identity_payload(), "source": source}
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
        source = self._source(request, verification)
        reference_identity = {**request.identity_payload(), "source": source}
        reference_id = f"sha256:{sha256_value(reference_identity)}"
        publication = self._publication_source or HttpPublicationSource(request.base_url)
        declared = publication.list_partitions(request)
        _validate_partition_catalog(declared, request)
        with AtomicDirectory(layout.staging) as staging:
            metadata = self._write_metadata(staging.path, verification)
            partition_records = []
            for item in sorted(declared, key=lambda value: value.month):
                payload = publication.download(item)
                _verify_partition(item, payload)
                relative = Path("bars") / f"month={item.month}" / "part-000.parquet"
                target = staging.path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                partition_records.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": item.sha256,
                        "content_bytes": item.content_bytes,
                        "month": item.month,
                    }
                )
            identity = {
                "source_snapshot_ref": reference_id,
                "partitions": partition_records,
                "catalog": metadata["catalog"],
                "calendar": metadata["calendar"],
                "coverage": metadata["coverage"],
            }
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
                return ResolvedSnapshot(
                    existing,
                    (final / "manifest.json").resolve(),
                    verification.dataset,
                )
            published = staging.publish(final)
            return ResolvedSnapshot(
                manifest,
                (published / "manifest.json").resolve(),
                verification.dataset,
            )

    def _source(
        self,
        request: SnapshotRequest,
        verification: SnapshotVerification | None,
    ) -> dict[str, Any]:
        revision = None
        if verification is not None:
            revision = f"{verification.dataset.data_version}:{verification.dataset.dataset_version}"
        return {
            "adapter": self.name,
            "adapter_version": self.adapter_version,
            "endpoint_contract": request.endpoint_contract,
            "base_url": request.base_url,
            "data_revision": revision,
        }

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
    actual = tuple(item.month for item in sorted(partitions, key=lambda item: item.month))
    if actual != expected:
        raise MarketHubContractError(
            f"published Parquet months do not cover request: expected={expected}, actual={actual}"
        )
    if any(
        item.content_bytes < 1 or len(item.sha256) != 64 or not item.download_url or not item.path
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
        pq.read_schema(pa.BufferReader(payload))
    except Exception as exc:
        raise MarketHubContractError(
            f"published partition {partition.month} is not valid Parquet: {exc}"
        ) from exc


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
