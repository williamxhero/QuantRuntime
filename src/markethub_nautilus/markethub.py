from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .canonical import CanonicalBar, CanonicalDataError, CanonicalDataset, CanonicalInstrument


class MarketHubError(RuntimeError):
    """A fail-closed MarketHub access or response-contract failure."""


@dataclass(slots=True)
class FetchMetrics:
    request_count: int = 0
    response_bytes: int = 0
    fetch_seconds: float = 0.0
    decode_seconds: float = 0.0
    catalog_rows: int = 0
    calendar_rows: int = 0
    bar_rows: int = 0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "bar_rows": self.bar_rows,
            "calendar_rows": self.calendar_rows,
            "catalog_rows": self.catalog_rows,
            "decode_seconds": self.decode_seconds,
            "fetch_seconds": self.fetch_seconds,
            "request_count": self.request_count,
            "response_bytes": self.response_bytes,
        }


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float, float]: ...


class UrllibJsonTransport:
    def __init__(self, base_url: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float, float]:
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = None
        headers = {"Accept": "application/json", "User-Agent": "markethub-nautilus/0.1"}
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        started = perf_counter()
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            detail = ""
            if isinstance(exc, HTTPError):
                detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise MarketHubError(f"{method} {path} failed: {exc}; {detail}") from exc
        fetch_seconds = perf_counter() - started
        decode_started = perf_counter()
        try:
            result = json.loads(raw, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise MarketHubError(f"{method} {path} returned invalid JSON") from exc
        return result, len(raw), fetch_seconds, perf_counter() - decode_started


class MarketHubClient:
    def __init__(
        self,
        base_url: str = "http://yosef-server:8803",
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibJsonTransport(self.base_url)
        self.metrics = FetchMetrics()
        self.data_version: str | None = None

    def open(self) -> str:
        health = self._request("GET", "/api/health")
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise MarketHubError(f"MarketHub is not healthy: {health!r}")
        version = str(health.get("data_version", ""))
        if not version:
            raise MarketHubError("MarketHub health response has no data_version")
        self.data_version = version
        return version

    def verify_version(self) -> None:
        frozen = self._require_open()
        health = self._request("GET", "/api/health")
        current = str(health.get("data_version", ""))
        if current != frozen:
            raise MarketHubError(f"data_version drift: frozen={frozen}, current={current}")

    def fetch_catalog(self, *, page_size: int = 5000) -> tuple[CanonicalInstrument, ...]:
        version = self._require_open()
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                "/api/stocks/catalog",
                query={
                    "data_version": version,
                    "include_delisted": "true",
                    "limit": page_size,
                    "offset": offset,
                },
            )
            if not isinstance(page, list):
                raise MarketHubError("catalog response is not a list")
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        self.metrics.catalog_rows += len(rows)
        result = tuple(
            sorted(
                (CanonicalInstrument.from_catalog(row) for row in rows),
                key=lambda item: item.instrument,
            )
        )
        identities = [item.instrument for item in result]
        if len(identities) != len(set(identities)):
            raise CanonicalDataError("catalog contains duplicate instruments")
        return result

    def fetch_calendar(self, start_date: date, end_date: date) -> tuple[date, ...]:
        rows = self._request(
            "GET",
            "/api/markets/calendar/trading",
            query={
                "data_version": self._require_open(),
                "end_date": end_date.isoformat(),
                "exchange": "SSE",
                "is_open": "true",
                "start_date": start_date.isoformat(),
            },
        )
        if not isinstance(rows, list):
            raise MarketHubError("calendar response is not a list")
        result = tuple(date.fromisoformat(str(row["trade_date"])) for row in rows)
        if result != tuple(sorted(set(result))):
            raise CanonicalDataError("calendar is duplicated or out of order")
        self.metrics.calendar_rows += len(result)
        return result

    def fetch_daily(
        self,
        instruments: tuple[CanonicalInstrument, ...],
        start_date: date,
        end_date: date,
        *,
        page_size: int = 50_000,
    ) -> tuple[CanonicalBar, ...]:
        version = self._require_open()
        by_code = {item.raw_code: item for item in instruments}
        if not by_code:
            raise ValueError("codes universe cannot be empty")
        cursor: str | None = None
        bars: list[CanonicalBar] = []
        previous_key: tuple[str, str] | None = None
        while True:
            body: dict[str, Any] = {
                "codes": sorted(by_code),
                "data_version": version,
                "end_date": end_date.isoformat(),
                "freq": "1d",
                "page_size": page_size,
                "start_date": start_date.isoformat(),
                "universe": "codes",
            }
            if cursor is not None:
                body["cursor"] = cursor
            response = self._request("POST", "/api/stocks/quotes/daily-window/query", body=body)
            if not isinstance(response, dict):
                raise MarketHubError("daily-window response is not an object")
            items = response.get("items", [])
            meta = response.get("meta", {})
            if not isinstance(items, list) or not isinstance(meta, dict):
                raise MarketHubError("daily-window response shape is invalid")
            if str(meta.get("data_version", "")) != version:
                raise MarketHubError("daily-window response data_version mismatch")
            for flag in ("complete", "request_complete", "delivery_complete"):
                if meta.get(flag) is not True:
                    raise MarketHubError(f"daily-window {flag} is not true: {meta!r}")
            for row in items:
                key = str(row["trade_time"]), str(row["code"])
                if previous_key is not None and key <= previous_key:
                    issue = "duplicates" if key == previous_key else "out of contract order"
                    raise CanonicalDataError(f"MarketHub rows contain {issue}: {key}")
                previous_key = key
                bars.append(CanonicalBar.from_markethub(row, by_code))
            cursor = meta.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor:
                raise MarketHubError("daily-window returned an invalid cursor")
        returned = {item.instrument for item in bars}
        expected = {
            item.instrument
            for item in instruments
            if (item.list_date is None or item.list_date <= end_date)
            and (item.delist_date is None or item.delist_date >= start_date)
        }
        if missing := expected - returned:
            raise CanonicalDataError(
                f"daily-window returned no rows for in-range instruments: {sorted(missing)}"
            )
        bars.sort(key=lambda item: item.identity)
        result = tuple(bars)
        self.metrics.bar_rows += len(result)
        return result

    def fetch_dataset(
        self,
        canonical_instruments: tuple[str, ...],
        start_date: date,
        end_date: date,
    ) -> CanonicalDataset:
        version = self.data_version or self.open()
        catalog = self.fetch_catalog()
        requested = set(canonical_instruments)
        instruments = tuple(item for item in catalog if item.instrument in requested)
        found = {item.instrument for item in instruments}
        if found != requested:
            raise CanonicalDataError(f"catalog missing instruments: {sorted(requested - found)}")
        trading_days = self.fetch_calendar(start_date, end_date)
        bars = self.fetch_daily(instruments, start_date, end_date)
        self.verify_version()
        result = CanonicalDataset(
            data_version=version,
            timezone="Asia/Shanghai",
            instruments=instruments,
            trading_days=trading_days,
            bars=bars,
        )
        result.validate()
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        decoded, size, fetch_seconds, decode_seconds = self.transport.request_json(
            method, path, query=query, body=body
        )
        self.metrics.request_count += 1
        self.metrics.response_bytes += size
        self.metrics.fetch_seconds += fetch_seconds
        self.metrics.decode_seconds += decode_seconds
        return decoded

    def _require_open(self) -> str:
        if self.data_version is None:
            raise MarketHubError("client is not open")
        return self.data_version
