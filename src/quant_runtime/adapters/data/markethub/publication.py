from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

import httpx

from quant_runtime.market_data.markethub.client import MarketHubContractError
from quant_runtime.sdk.snapshot_contract import SnapshotRequest


@dataclass(frozen=True, slots=True)
class PublishedPartition:
    month: str
    path: str
    content_bytes: int
    sha256: str
    download_url: str


class PublicationSource(Protocol):
    def list_partitions(self, request: SnapshotRequest) -> tuple[PublishedPartition, ...]: ...

    def download(self, partition: PublishedPartition) -> bytes: ...


class HttpPublicationSource:
    """Consume MarketHub's publication catalog without transforming published bytes."""

    def __init__(self, base_url: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def list_partitions(self, request: SnapshotRequest) -> tuple[PublishedPartition, ...]:
        payload = self._json(
            "/api/data-publications/stock-bar-1d/monthly",
            params={
                "start_month": request.start.strftime("%Y-%m"),
                "end_month": request.end.strftime("%Y-%m"),
                "frequency": request.frequency,
                "adjustment": request.adjustment,
            },
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise MarketHubContractError("publication catalog items must be a list of objects")
        try:
            return tuple(
                PublishedPartition(
                    month=str(item["month"]),
                    path=str(item["path"]),
                    content_bytes=int(item["content_bytes"]),
                    sha256=str(item["sha256"]),
                    download_url=str(item["download_url"]),
                )
                for item in items
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketHubContractError(f"invalid publication catalog: {exc}") from exc

    def download(self, partition: PublishedPartition) -> bytes:
        url = (
            partition.download_url
            if partition.download_url.startswith(("http://", "https://"))
            else f"{self._base_url}/{partition.download_url.lstrip('/')}"
        )
        try:
            response = httpx.get(
                url,
                timeout=self._timeout_seconds,
                headers={"Accept": "application/vnd.apache.parquet"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MarketHubContractError(f"published Parquet download failed: {exc}") from exc
        return response.content

    def _json(self, path: str, *, params: dict[str, Any]) -> Any:
        started = perf_counter()
        try:
            response = httpx.get(
                f"{self._base_url}{path}",
                params=params,
                timeout=self._timeout_seconds,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return json.loads(response.content)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MarketHubContractError(
                f"publication catalog request failed after {perf_counter() - started:.3f}s: {exc}"
            ) from exc
