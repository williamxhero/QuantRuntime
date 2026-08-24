from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

import httpx

from .calendar import canonical_trading_days
from .catalog import CanonicalInstrument
from .lineage import HealthVector
from .model import CanonicalBar, CanonicalDataset


class MarketHubContractError(RuntimeError):
    """MarketHub could not satisfy the frozen read contract."""


@dataclass(slots=True)
class FetchMetrics:
    request_count: int = 0
    response_bytes: int = 0
    fetch_seconds: float = 0.0
    catalog_rows: int = 0
    calendar_rows: int = 0
    daily_rows: int = 0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "request_count": self.request_count,
            "response_bytes": self.response_bytes,
            "fetch_seconds": self.fetch_seconds,
            "catalog_rows": self.catalog_rows,
            "calendar_rows": self.calendar_rows,
            "daily_rows": self.daily_rows,
        }


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float]: ...


class HttpxJsonTransport:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float]:
        started = perf_counter()
        try:
            response = httpx.request(
                method,
                f"{self._base_url}{path}",
                params=query,
                json=body,
                timeout=self._timeout_seconds,
                headers={"Accept": "application/json", "User-Agent": "quant-runtime/0.2"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = exc.response.text[:2000]
            raise MarketHubContractError(f"{method} {path} failed: {exc}; {detail}") from exc
        elapsed = perf_counter() - started
        try:
            decoded = json.loads(response.content, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise MarketHubContractError(f"{method} {path} returned invalid JSON") from exc
        return decoded, len(response.content), elapsed


class MarketHubClient:
    def __init__(
        self,
        base_url: str = "http://yosef-server:8803",
        *,
        timeout_seconds: float = 60.0,
        transport: JsonTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport or HttpxJsonTransport(self.base_url, timeout_seconds)
        self._health: HealthVector | None = None
        self.metrics = FetchMetrics()

    def open(self) -> HealthVector:
        self._health = self._read_health()
        return self._health

    def verify_version(self) -> None:
        frozen = self._require_open()
        current = self._read_health()
        if current != frozen:
            raise MarketHubContractError(f"MarketHub version drift: {frozen!r} -> {current!r}")

    def fetch_dataset(
        self,
        instruments: tuple[str, ...],
        start_date: date,
        end_date: date,
        *,
        page_size: int = 50_000,
    ) -> CanonicalDataset:
        frozen = self._health or self.open()
        catalog = self.fetch_catalog()
        requested = set(instruments)
        selected = tuple(item for item in catalog if item.instrument in requested)
        found = {item.instrument for item in selected}
        if found != requested:
            raise MarketHubContractError(f"catalog lacks instruments: {sorted(requested - found)}")
        trading_days = self.fetch_calendar(start_date, end_date)
        rows = self.fetch_daily(
            codes=tuple(sorted(item.raw_code for item in selected)),
            start_date=start_date,
            end_date=end_date,
            page_size=page_size,
        )
        by_code = {item.raw_code: item for item in selected}
        try:
            bars = tuple(
                sorted(
                    (CanonicalBar.from_markethub(row, by_code) for row in rows),
                    key=lambda item: item.identity,
                )
            )
        except (KeyError, ValueError) as exc:
            raise MarketHubContractError(f"invalid canonical daily data: {exc}") from exc
        returned = {item.instrument for item in bars}
        expected = {
            item.instrument
            for item in selected
            if (item.list_date is None or item.list_date <= end_date)
            and (item.delist_date is None or item.delist_date >= start_date)
        }
        if missing := expected - returned:
            raise MarketHubContractError(f"daily-window has no rows for {sorted(missing)}")
        self.verify_version()
        dataset = CanonicalDataset(
            data_version=frozen.data_version,
            dataset_version=frozen.daily_dataset_version,
            timezone="Asia/Shanghai",
            instruments=selected,
            trading_days=trading_days,
            bars=bars,
        )
        try:
            dataset.validate()
        except ValueError as exc:
            raise MarketHubContractError(f"canonical dataset validation failed: {exc}") from exc
        return dataset

    def fetch_catalog(self, *, page_size: int = 5000) -> tuple[CanonicalInstrument, ...]:
        version = self._require_open().data_version
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                "/api/stocks/catalog",
                query={
                    "include_delisted": "true",
                    "limit": page_size,
                    "offset": offset,
                    "data_version": version,
                },
            )
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise MarketHubContractError("catalog response must be a list of objects")
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        try:
            catalog = tuple(
                sorted(
                    (CanonicalInstrument.from_catalog(row) for row in rows),
                    key=lambda item: item.instrument,
                )
            )
        except ValueError as exc:
            raise MarketHubContractError(f"invalid catalog: {exc}") from exc
        identities = [item.instrument for item in catalog]
        if len(identities) != len(set(identities)):
            raise MarketHubContractError("catalog contains duplicate instruments")
        self.metrics.catalog_rows += len(catalog)
        return catalog

    def fetch_calendar(self, start_date: date, end_date: date) -> tuple[date, ...]:
        rows = self._request(
            "GET",
            "/api/markets/calendar/trading",
            query={
                "exchange": "SSE",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "is_open": "true",
                "data_version": self._require_open().data_version,
            },
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MarketHubContractError("calendar response must be a list of objects")
        try:
            days = canonical_trading_days(rows)
        except ValueError as exc:
            raise MarketHubContractError(str(exc)) from exc
        self.metrics.calendar_rows += len(days)
        return days

    def fetch_daily(
        self,
        *,
        codes: tuple[str, ...],
        start_date: date,
        end_date: date,
        page_size: int,
    ) -> tuple[dict[str, Any], ...]:
        frozen = self._require_open()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        rows: list[dict[str, Any]] = []
        previous_key: tuple[str, str] | None = None
        total_rows: int | None = None
        while True:
            body: dict[str, Any] = {
                "codes": list(codes),
                "data_version": frozen.data_version,
                "dataset_version": frozen.daily_dataset_version,
                "freq": "1d",
                "universe": "codes",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "page_size": page_size,
                "meta_detail": "full",
            }
            if cursor is not None:
                body["cursor"] = cursor
            response = self._request("POST", "/api/stocks/quotes/daily-window/query", body=body)
            if not isinstance(response, dict):
                raise MarketHubContractError("daily-window response must be an object")
            page = response.get("items")
            meta = response.get("meta")
            if not isinstance(page, list) or not isinstance(meta, dict):
                raise MarketHubContractError("daily-window response shape is invalid")
            self._validate_daily_meta(meta, page, frozen)
            reported_total = int(meta["total_rows"])
            if total_rows is None:
                total_rows = reported_total
            elif total_rows != reported_total:
                raise MarketHubContractError("daily-window total_rows changed during pagination")
            for row in page:
                if not isinstance(row, dict):
                    raise MarketHubContractError("daily-window item must be an object")
                key = str(row.get("trade_time", "")), str(row.get("code", ""))
                if not all(key):
                    raise MarketHubContractError("daily-window item lacks identity")
                if previous_key is not None and key <= previous_key:
                    raise MarketHubContractError(f"daily-window ordering violation at {key}")
                previous_key = key
                rows.append(row)
            next_cursor = meta.get("next_cursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise MarketHubContractError("daily-window cursor is invalid or repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if total_rows is None or len(rows) != total_rows:
            raise MarketHubContractError(
                f"daily-window delivered {len(rows)} rows but declared {total_rows}"
            )
        self.metrics.daily_rows += len(rows)
        return tuple(rows)

    def _read_health(self) -> HealthVector:
        response = self._request("GET", "/api/health")
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise MarketHubContractError(f"MarketHub is unhealthy: {response!r}")
        versions = response.get("dataset_versions")
        if not isinstance(versions, dict):
            raise MarketHubContractError("health response lacks dataset_versions")
        health = HealthVector(
            data_version=str(response.get("data_version", "")),
            daily_dataset_version=str(versions.get("stock_daily_1d", "")),
        )
        if not health.data_version or not health.daily_dataset_version:
            raise MarketHubContractError("health response lacks required versions")
        return health

    def _validate_daily_meta(
        self,
        meta: dict[str, Any],
        page: list[Any],
        frozen: HealthVector,
    ) -> None:
        if str(meta.get("data_version", "")) != frozen.data_version:
            raise MarketHubContractError("daily-window data_version mismatch")
        if str(meta.get("dataset_version", "")) != frozen.daily_dataset_version:
            raise MarketHubContractError("daily-window dataset_version mismatch")
        if meta.get("universe_kind") != "codes":
            raise MarketHubContractError("daily-window universe mismatch")
        if meta.get("truncated") is not False:
            raise MarketHubContractError("daily-window response is truncated")
        for flag in ("complete", "page_complete", "request_complete", "delivery_complete"):
            if meta.get(flag) is not True:
                raise MarketHubContractError(f"daily-window {flag} is not true")
        if int(meta.get("returned_rows", -1)) != len(page):
            raise MarketHubContractError("daily-window returned_rows mismatch")
        coverage = meta.get("coverage")
        if not isinstance(coverage, list):
            raise MarketHubContractError("daily-window full coverage is missing")
        for item in coverage:
            if not isinstance(item, dict):
                raise MarketHubContractError("daily-window coverage item is invalid")
            if item.get("complete") is not True or int(item.get("missing_rows", -1)) != 0:
                raise MarketHubContractError(f"daily-window coverage is incomplete: {item!r}")
            if int(item.get("expected_rows", -1)) != int(item.get("actual_rows", -2)):
                raise MarketHubContractError(f"daily-window coverage counts differ: {item!r}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        decoded, response_bytes, elapsed = self._transport.request_json(
            method, path, query=query, body=body
        )
        self.metrics.request_count += 1
        self.metrics.response_bytes += response_bytes
        self.metrics.fetch_seconds += elapsed
        return decoded

    def _require_open(self) -> HealthVector:
        if self._health is None:
            raise MarketHubContractError("MarketHub client is not open")
        return self._health
