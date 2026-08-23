from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from quant_runtime.contracts.canonical_hash import sha256_bytes
from quant_runtime.market_data.markethub.client import MarketHubContractError
from quant_runtime.sdk.snapshot_contract import SnapshotRequest

EXPORT_SCHEMA = "markethub-stock-daily-parquet-v1"
PARTITION_PATH = re.compile(
    r"^year=(?P<year>[0-9]{4})/month=(?P<month>[0-9]{2})/"
    r"(?P<kind>bars|coverage)\.parquet$"
)


@dataclass(frozen=True, slots=True)
class PublishedPartition:
    month: str
    kind: str
    path: str
    rows: int
    content_bytes: int
    sha256: str
    download_url: str


class PublicationSource(Protocol):
    def list_partitions(
        self,
        request: SnapshotRequest,
        *,
        market_data_version: str,
        dataset_version: str,
    ) -> tuple[PublishedPartition, ...]: ...

    def download(self, partition: PublishedPartition) -> bytes: ...


class HttpPublicationSource:
    """Consume the immutable MarketHub stock_daily_1d export contract."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 60.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def list_partitions(
        self,
        request: SnapshotRequest,
        *,
        market_data_version: str,
        dataset_version: str,
    ) -> tuple[PublishedPartition, ...]:
        mapping, _ = self._json(
            "/api/exports/stock_daily_1d/resolve/" + quote(market_data_version, safe=""),
        )
        expected_mapping = {
            "dataset_id": "stock_daily_1d",
            "market_data_version": market_data_version,
            "dataset_version": dataset_version,
        }
        if any(mapping.get(key) != value for key, value in expected_mapping.items()):
            raise MarketHubContractError(
                f"MarketHub export mapping does not match frozen versions: {mapping!r}"
            )
        manifest_url = str(mapping.get("manifest_url", ""))
        manifest_sha256 = str(mapping.get("manifest_sha256", ""))
        if not manifest_url or len(manifest_sha256) != 64:
            raise MarketHubContractError("MarketHub export mapping lacks manifest integrity")
        manifest, manifest_bytes = self._json(manifest_url)
        if sha256_bytes(manifest_bytes) != manifest_sha256:
            raise MarketHubContractError("MarketHub export manifest sha256 mismatch")
        if (
            manifest.get("schema_version") != EXPORT_SCHEMA
            or manifest.get("dataset_id") != "stock_daily_1d"
            or manifest.get("dataset_version") != dataset_version
            or manifest.get("market_data_version") != market_data_version
        ):
            raise MarketHubContractError("MarketHub export manifest identity mismatch")
        files = manifest.get("files")
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise MarketHubContractError("MarketHub export manifest files are invalid")
        expected_months = set(_months(request))
        selected = []
        for item in files:
            match = PARTITION_PATH.fullmatch(str(item.get("path", "")))
            if match is None:
                continue
            month = f"{match.group('year')}-{match.group('month')}"
            if month not in expected_months:
                continue
            try:
                selected.append(
                    PublishedPartition(
                        month=month,
                        kind=match.group("kind"),
                        path=str(item["path"]),
                        rows=int(item["rows"]),
                        content_bytes=int(item["bytes"]),
                        sha256=str(item["sha256"]),
                        download_url=str(item["url"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketHubContractError(f"invalid export file entry: {item!r}") from exc
        return tuple(selected)

    def download(self, partition: PublishedPartition) -> bytes:
        response = self._get(partition.download_url, accept="application/vnd.apache.parquet")
        return response.content

    def _json(self, path: str) -> tuple[dict[str, Any], bytes]:
        response = self._get(path, accept="application/json")
        try:
            value = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise MarketHubContractError(f"MarketHub export returned invalid JSON: {path}") from exc
        if not isinstance(value, dict):
            raise MarketHubContractError(f"MarketHub export JSON root must be an object: {path}")
        return value, response.content

    def _get(self, path: str, *, accept: str) -> httpx.Response:
        url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self._base_url}/{path.lstrip('/')}"
        )
        try:
            response = self._client.get(url, headers={"Accept": accept})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MarketHubContractError(f"MarketHub export request failed: {exc}") from exc
        return response


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
