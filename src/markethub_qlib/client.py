from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

import httpx
import pandas as pd

from .canonical import hash_frame


class MarketHubContractError(RuntimeError):
    """Raised when MarketHub cannot satisfy the frozen read contract."""


@dataclass(frozen=True, slots=True)
class HealthVector:
    data_version: str
    daily_dataset_version: str


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


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    frame: pd.DataFrame
    data_version: str
    dataset_version: str
    canonical_input_hash: str
    trading_days: tuple[date, ...]
    instruments: tuple[str, ...]
    metrics: dict[str, int | float] = field(default_factory=dict)


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
                headers={"Accept": "application/json", "User-Agent": "markethub-qlib/0.1"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = exc.response.text[:1000]
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
        health = self._read_health()
        self._health = health
        return health

    def verify_version(self) -> None:
        frozen = self._require_open()
        current = self._read_health()
        if current != frozen:
            raise MarketHubContractError(
                "MarketHub version drift: "
                f"frozen={frozen.data_version}/{frozen.daily_dataset_version}, "
                f"current={current.data_version}/{current.daily_dataset_version}"
            )

    def fetch_catalog(
        self,
        *,
        include_delisted: bool = True,
        page_size: int = 5000,
    ) -> tuple[dict[str, Any], ...]:
        version = self._require_open().data_version
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                "/api/stocks/catalog",
                query={
                    "include_delisted": str(include_delisted).lower(),
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
        codes = [str(row.get("code", "")) for row in rows]
        if any(len(code) != 6 or not code.isdigit() for code in codes):
            raise MarketHubContractError("catalog contains an invalid A-share code")
        if len(codes) != len(set(codes)):
            raise MarketHubContractError("catalog contains duplicate codes")
        self.metrics.catalog_rows += len(rows)
        return tuple(sorted(rows, key=lambda row: str(row["code"])))

    def fetch_calendar(self, start_date: date, end_date: date) -> tuple[date, ...]:
        version = self._require_open().data_version
        rows = self._request(
            "GET",
            "/api/markets/calendar/trading",
            query={
                "exchange": "SSE",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "is_open": "true",
                "data_version": version,
            },
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MarketHubContractError("calendar response must be a list of objects")
        try:
            days = tuple(date.fromisoformat(str(row["trade_date"])) for row in rows)
        except (KeyError, ValueError) as exc:
            raise MarketHubContractError("calendar contains an invalid date") from exc
        if days != tuple(sorted(set(days))):
            raise MarketHubContractError("calendar is duplicated or out of order")
        if any(row.get("is_open") is not True for row in rows):
            raise MarketHubContractError("calendar contains a closed day")
        self.metrics.calendar_rows += len(days)
        return days

    def fetch_daily(
        self,
        *,
        universe_kind: str,
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
                "data_version": frozen.data_version,
                "dataset_version": frozen.daily_dataset_version,
                "freq": "1d",
                "universe": universe_kind,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "page_size": page_size,
                "meta_detail": "full",
            }
            if universe_kind == "codes":
                body["codes"] = list(codes)
            if cursor is not None:
                body["cursor"] = cursor
            response = self._request("POST", "/api/stocks/quotes/daily-window/query", body=body)
            if not isinstance(response, dict):
                raise MarketHubContractError("daily-window response must be an object")
            page = response.get("items")
            meta = response.get("meta")
            if not isinstance(page, list) or not isinstance(meta, dict):
                raise MarketHubContractError("daily-window response shape is invalid")
            self._validate_daily_meta(meta, page, frozen, universe_kind)
            reported_total = int(meta["total_rows"])
            if total_rows is None:
                total_rows = reported_total
            elif total_rows != reported_total:
                raise MarketHubContractError("daily-window total_rows changed during pagination")
            for row in page:
                if not isinstance(row, dict):
                    raise MarketHubContractError("daily-window item must be an object")
                try:
                    key = (str(row["trade_time"]), str(row["code"]))
                except KeyError as exc:
                    raise MarketHubContractError("daily-window item lacks identity") from exc
                if previous_key is not None and key <= previous_key:
                    raise MarketHubContractError(
                        f"daily-window rows are duplicated or out of order at {key}"
                    )
                previous_key = key
                rows.append(row)
            next_cursor = meta.get("next_cursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MarketHubContractError("daily-window cursor is not opaque non-empty text")
            if next_cursor in seen_cursors:
                raise MarketHubContractError("daily-window cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if total_rows is None or len(rows) != total_rows:
            raise MarketHubContractError(
                f"daily-window delivered {len(rows)} rows but declared {total_rows}"
            )
        self.metrics.daily_rows += len(rows)
        return tuple(rows)

    def load_qlib_frame(
        self,
        *,
        universe_kind: str,
        codes: tuple[str, ...],
        start_date: date,
        end_date: date,
        fields: tuple[str, ...],
        page_size: int = 50_000,
    ) -> LoadedDataset:
        frozen = self._health or self.open()
        catalog = self.fetch_catalog(include_delisted=True)
        catalog_by_code = {str(row["code"]): row for row in catalog}
        if universe_kind == "codes":
            missing = set(codes) - catalog_by_code.keys()
            if missing:
                raise MarketHubContractError(f"catalog lacks requested codes: {sorted(missing)}")
        trading_days = self.fetch_calendar(start_date, end_date)
        rows = self.fetch_daily(
            universe_kind=universe_kind,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size,
        )
        self.verify_version()
        frame = _to_qlib_frame(rows, catalog_by_code, fields, set(trading_days))
        instruments = tuple(sorted(set(frame.index.get_level_values("instrument"))))
        canonical_input_hash = hash_frame(
            frame,
            data_version=frozen.data_version,
            dataset_version=frozen.daily_dataset_version,
        )
        return LoadedDataset(
            frame=frame,
            data_version=frozen.data_version,
            dataset_version=frozen.daily_dataset_version,
            canonical_input_hash=canonical_input_hash,
            trading_days=trading_days,
            instruments=instruments,
            metrics=self.metrics.as_dict(),
        )

    def _read_health(self) -> HealthVector:
        response = self._request("GET", "/api/health")
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise MarketHubContractError(f"MarketHub is unhealthy: {response!r}")
        data_version = str(response.get("data_version", ""))
        dataset_versions = response.get("dataset_versions")
        if not isinstance(dataset_versions, dict):
            raise MarketHubContractError("health response lacks dataset_versions")
        daily_dataset_version = str(dataset_versions.get("stock_daily_1d", ""))
        if not data_version or not daily_dataset_version:
            raise MarketHubContractError("health response lacks required versions")
        return HealthVector(data_version, daily_dataset_version)

    def _validate_daily_meta(
        self,
        meta: dict[str, Any],
        page: list[Any],
        frozen: HealthVector,
        universe_kind: str,
    ) -> None:
        if str(meta.get("data_version", "")) != frozen.data_version:
            raise MarketHubContractError("daily-window data_version mismatch")
        if str(meta.get("dataset_version", "")) != frozen.daily_dataset_version:
            raise MarketHubContractError("daily-window dataset_version mismatch")
        if meta.get("universe_kind") != universe_kind:
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


def _to_qlib_frame(
    rows: tuple[dict[str, Any], ...],
    catalog_by_code: dict[str, dict[str, Any]],
    fields: tuple[str, ...],
    trading_days: set[date],
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["code"])
        catalog = catalog_by_code.get(code)
        if catalog is None:
            raise MarketHubContractError(f"daily-window returned unknown code {code}")
        instrument = _instrument_id(code, str(catalog.get("exchange", "")))
        timestamp = pd.Timestamp(str(row["trade_time"])).normalize()
        if timestamp.date() not in trading_days:
            raise MarketHubContractError(f"bar {timestamp.date()} is absent from trading calendar")
        record: dict[str, Any] = {"datetime": timestamp, "instrument": instrument}
        is_suspended = bool(row.get("is_suspended", False))
        record["is_suspended"] = is_suspended
        record["is_st"] = bool(row.get("is_st", False))
        for field_name in fields:
            if field_name in {"is_suspended", "is_st"}:
                continue
            if field_name not in row:
                raise MarketHubContractError(f"daily-window row lacks field {field_name}")
            value = row[field_name]
            if value is None and is_suspended:
                record[field_name] = float("nan")
                continue
            if value is None:
                raise MarketHubContractError(
                    f"daily-window row has null {field_name}: {timestamp.date()}/{code}"
                )
            number = Decimal(str(value))
            if not number.is_finite():
                raise MarketHubContractError(
                    f"daily-window row has non-finite {field_name}: {timestamp.date()}/{code}"
                )
            record[field_name] = float(number)
        records.append(record)
    if not records:
        raise MarketHubContractError("daily-window returned no bars")
    frame = pd.DataFrame.from_records(records).set_index(["datetime", "instrument"])
    frame = frame.sort_index()
    if not frame.index.is_unique:
        raise MarketHubContractError("Qlib frame index contains duplicates")
    frame.index = frame.index.set_names(["datetime", "instrument"])
    return frame


def _instrument_id(code: str, exchange: str) -> str:
    prefix_by_exchange = {
        "SSE": "SH",
        "SHSE": "SH",
        "SZSE": "SZ",
        "BSE": "BJ",
        "BJSE": "BJ",
    }
    try:
        prefix = prefix_by_exchange[exchange.upper()]
    except KeyError as exc:
        raise MarketHubContractError(f"unsupported exchange {exchange!r}") from exc
    return f"{prefix}.{code}"
