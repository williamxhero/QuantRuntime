from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from time import perf_counter
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from .calendar import canonical_trading_days
from .catalog import CanonicalInstrument
from .contract import PartialFuturesPublication
from .futures_model import (
    CanonicalFuturesBarChunk,
    CanonicalFuturesBars,
    CanonicalFuturesDataset,
    CanonicalFuturesInstrument,
    FuturesContractCatalogIdentity,
    product_code_from_instrument,
)
from .lineage import HealthVector
from .model import CanonicalBar, CanonicalDataset

SHANGHAI = ZoneInfo("Asia/Shanghai")
FUTURES_PAGE_SIZE = 500_000
FUTURES_DATASET_PAGE_SIZE = 200_000
PARTIAL_FUTURES_PAGE_SIZE = 10_000


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
        value, response_bytes, elapsed, _ = self.request_json_with_headers(
            method, path, query=query, body=body
        )
        return value, response_bytes, elapsed

    def request_json_with_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float, dict[str, str]]:
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
        return decoded, len(response.content), elapsed, dict(response.headers)


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

    def verify_version(
        self,
        frequency: str = "1d",
        *,
        include_futures_contracts: bool = False,
    ) -> None:
        frozen = self._require_open()
        current = self._read_health()
        if frequency == "1m":
            stable = current.futures_1m_dataset_version == frozen.futures_1m_dataset_version
            if include_futures_contracts:
                stable = stable and (
                    current.futures_contract_dataset_version
                    == frozen.futures_contract_dataset_version
                )
        else:
            stable = (
                current.data_version == frozen.data_version
                and current.daily_dataset_version == frozen.daily_dataset_version
            )
        if not stable:
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

    def fetch_futures_dataset(
        self,
        instruments: tuple[str, ...],
        start_date: date,
        end_date: date,
        *,
        series_type: str,
        include_contract_catalog: bool = True,
    ) -> CanonicalFuturesDataset:
        if series_type not in {"back_adjusted_continuous", "main_continuous"}:
            raise MarketHubContractError(f"unsupported futures series_type {series_type!r}")
        frozen = self._health or self.open()
        if not frozen.futures_1m_dataset_version:
            raise MarketHubContractError("health response lacks future_bar_1m dataset version")
        product_by_instrument = {
            instrument: product_code_from_instrument(instrument) for instrument in instruments
        }
        casefolded = [item.casefold() for item in product_by_instrument.values()]
        if len(casefolded) != len(set(casefolded)):
            raise MarketHubContractError("futures instruments map to duplicate product codes")
        catalog_identity: FuturesContractCatalogIdentity | None = None
        catalog_specs: dict[str, dict[str, Any]] = {}
        if include_contract_catalog:
            catalog_identity, catalog_specs = self.fetch_futures_contracts(
                tuple(product_by_instrument.values())
            )
        coverage = self.fetch_futures_coverage(series_type=series_type)
        coverage_by_product = {
            str(item.get("product_code", "")).casefold(): item for item in coverage
        }
        missing = sorted(
            product
            for product in product_by_instrument.values()
            if product.casefold() not in coverage_by_product
        )
        if missing:
            raise MarketHubContractError(f"futures coverage lacks products: {missing}")

        chunks_by_instrument: dict[str, tuple[CanonicalFuturesBarChunk, ...]] = {}
        native_instruments: list[CanonicalFuturesInstrument] = []
        for instrument, product in product_by_instrument.items():
            coverage_item = coverage_by_product[product.casefold()]
            if str(coverage_item.get("series_type", "")) != series_type:
                raise MarketHubContractError(f"futures coverage series drifted for {instrument!r}")
            chunks: list[CanonicalFuturesBarChunk] = []
            native_instrument: CanonicalFuturesInstrument | None = None
            for page in self._iter_futures_1m_pages(
                product_code=product,
                series_type=series_type,
                start_date=start_date,
                end_date=end_date,
                page_size=FUTURES_DATASET_PAGE_SIZE,
            ):
                if native_instrument is None:
                    catalog_spec = catalog_specs.get(product.casefold())
                    if (
                        catalog_spec is not None
                        and str(page[0].get("exchange", "")) != catalog_spec["exchange"]
                    ):
                        raise MarketHubContractError(
                            f"futures bar/catalog exchange mismatch for {instrument!r}"
                        )
                    native_instrument = CanonicalFuturesInstrument(
                        instrument=instrument,
                        product_code=product,
                        exchange=str(page[0].get("exchange", "")),
                        series_type=series_type,
                        **_catalog_native_fields(catalog_specs.get(product.casefold())),
                    )
                try:
                    chunks.append(
                        CanonicalFuturesBarChunk.from_rows(
                            page,
                            {product.casefold(): native_instrument},
                            parse_time=_parse_futures_time,
                        )
                    )
                except ValueError as exc:
                    raise MarketHubContractError(f"invalid canonical futures data: {exc}") from exc
            if not chunks or native_instrument is None:
                raise MarketHubContractError(f"futures 1m has no rows for {instrument!r}")
            native_instruments.append(native_instrument)
            chunks_by_instrument[instrument] = tuple(chunks)

        native_instruments_tuple = tuple(
            sorted(native_instruments, key=lambda item: item.instrument)
        )
        bars = CanonicalFuturesBars(
            tuple(chunks_by_instrument[item.instrument] for item in native_instruments_tuple)
        )
        self.verify_version(
            "1m",
            include_futures_contracts=include_contract_catalog,
        )
        dataset = CanonicalFuturesDataset(
            data_version="future_bar_1m",
            dataset_version=frozen.futures_1m_dataset_version,
            timezone="Asia/Shanghai",
            series_type=series_type,
            instruments=native_instruments_tuple,
            bars=bars,
            contract_catalog=catalog_identity,
        )
        try:
            dataset.validate()
        except ValueError as exc:
            raise MarketHubContractError(
                f"canonical futures dataset validation failed: {exc}"
            ) from exc
        return dataset

    def fetch_partial_futures_dataset(
        self,
        instruments: tuple[str, ...],
        start_date: date,
        end_date: date,
        *,
        series_type: str,
        publication: PartialFuturesPublication,
    ) -> CanonicalFuturesDataset:
        if series_type != "back_adjusted_continuous":
            raise MarketHubContractError("partial futures only supports back_adjusted_continuous")
        products = {item: product_code_from_instrument(item) for item in instruments}
        if len({item.casefold() for item in products.values()}) != len(products):
            raise MarketHubContractError("futures instruments map to duplicate product codes")
        _, coverage = self._partial_coverage(
            publication, tuple(products.values()), start_date, end_date
        )
        coverage_by_product: dict[str, list[dict[str, Any]]] = {}
        for item in coverage:
            coverage_by_product.setdefault(str(item.get("product_code", "")).casefold(), []).append(
                item
            )
        chunks_by_instrument: dict[str, tuple[CanonicalFuturesBarChunk, ...]] = {}
        native_instruments: list[CanonicalFuturesInstrument] = []
        lineage_meta: dict[str, Any] | None = None
        for instrument, product in products.items():
            if product.casefold() not in coverage_by_product:
                raise MarketHubContractError(f"partial futures coverage lacks product {product!r}")
            coverage_items = coverage_by_product[product.casefold()]
            if not all(
                item.get("status") == "accepted" and int(item.get("observed_count", 0)) > 0
                for item in coverage_items
            ):
                raise MarketHubContractError(
                    f"partial futures coverage is unusable for {product!r}"
                )
            pages = self._iter_partial_futures_pages(publication, product, start_date, end_date)
            chunks: list[CanonicalFuturesBarChunk] = []
            native: CanonicalFuturesInstrument | None = None
            for rows, metadata in pages:
                if lineage_meta is None:
                    lineage_meta = metadata
                elif _partial_identity(metadata) != _partial_identity(lineage_meta):
                    raise MarketHubContractError("partial futures lineage drifted during read")
                if native is None:
                    native = CanonicalFuturesInstrument(
                        instrument=instrument,
                        product_code=product,
                        exchange=str(rows[0].get("exchange", "")),
                        series_type=series_type,
                    )
                self._validate_partial_rows(rows, product)
                try:
                    chunks.append(
                        CanonicalFuturesBarChunk.from_rows(
                            rows, {product.casefold(): native}, parse_time=_parse_futures_time
                        )
                    )
                except ValueError as exc:
                    raise MarketHubContractError(
                        f"invalid canonical partial futures data: {exc}"
                    ) from exc
            if native is None or not chunks:
                raise MarketHubContractError(
                    f"partial futures has no observed rows for {instrument!r}"
                )
            native_instruments.append(native)
            chunks_by_instrument[instrument] = tuple(chunks)
        ordered = tuple(sorted(native_instruments, key=lambda item: item.instrument))
        dataset = CanonicalFuturesDataset(
            data_version=publication.dataset_id,
            dataset_version=publication.dataset_version,
            timezone="Asia/Shanghai",
            series_type=series_type,
            instruments=ordered,
            bars=CanonicalFuturesBars(
                tuple(chunks_by_instrument[item.instrument] for item in ordered)
            ),
            partial_lineage={
                "publication": publication.as_dict(),
                "qmi_id": _require_partial_lineage(lineage_meta, "qmi_id"),
                "catalog_identity": _require_partial_lineage(lineage_meta, "catalog_identity"),
                "source_boundary_manifest": _require_partial_lineage(
                    lineage_meta, "source_boundary_manifest"
                ),
                "source_manifests": _require_partial_lineage(lineage_meta, "source_manifests"),
                "lineage_limitations": _require_partial_lineage(
                    lineage_meta, "lineage_limitations"
                ),
                "missing_bar_semantics": "skip",
                "session_grid": "not_asserted_complete",
            },
        )
        try:
            dataset.validate()
        except ValueError as exc:
            raise MarketHubContractError(
                f"canonical partial futures dataset validation failed: {exc}"
            ) from exc
        return dataset

    def _partial_coverage(self, publication, products, start_date, end_date):
        response, headers = self._request_with_headers(
            "GET",
            "/api/futures/quotes/1m/partial/coverage",
            query=self._partial_query(
                publication, products, start_date, end_date, limit=PARTIAL_FUTURES_PAGE_SIZE
            ),
        )
        return self._validate_partial_response(response, headers, publication, "coverage")

    def _iter_partial_futures_pages(self, publication, product, start_date, end_date):
        cursor = None
        previous: datetime | None = None
        seen_cursors: set[str] = set()
        while True:
            query = self._partial_query(
                publication, (product,), start_date, end_date, limit=PARTIAL_FUTURES_PAGE_SIZE
            )
            if cursor is not None:
                query["cursor"] = cursor
            response, headers = self._request_with_headers(
                "GET", "/api/futures/quotes/1m/partial", query=query
            )
            meta, rows = self._validate_partial_response(response, headers, publication, "bars")
            for row in rows:
                timestamp = _parse_futures_time(row.get("bar_time"))
                if previous is not None and timestamp <= previous:
                    raise MarketHubContractError(
                        f"partial futures ordering violation at {product}/{timestamp.isoformat()}"
                    )
                previous = timestamp
            if rows:
                yield tuple(rows), meta
            next_cursor = meta.get("next_cursor")
            if next_cursor is None:
                return
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise MarketHubContractError("partial futures cursor is invalid or repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _partial_query(publication, products, start_date, end_date, *, limit):
        return {
            **publication.as_dict(),
            "codes": ",".join(products),
            "start_time": f"{start_date.isoformat()} 00:00:00",
            "end_time": f"{end_date.isoformat()} 23:59:00",
            "limit": limit,
        }

    def _validate_partial_response(self, response, headers, publication, kind):
        if not isinstance(response, dict) or not isinstance(response.get("meta"), dict):
            raise MarketHubContractError(f"partial futures {kind} response shape is invalid")
        meta = response["meta"]
        if meta.get("partial_contract_satisfied") is not True:
            raise MarketHubContractError("partial futures contract is not satisfied")
        if meta.get("missing_bar_semantics") != "skip":
            raise MarketHubContractError("partial futures missing-bar semantics drifted")
        if kind == "bars" and meta.get("session_grid") != "not_asserted_complete":
            raise MarketHubContractError("partial futures session grid drifted")
        for key, expected in publication.as_dict().items():
            if str(meta.get(key, "")) != expected:
                raise MarketHubContractError(f"partial futures {key} mismatch")
        for header, expected in {
            "x-markethub-dataset-version": publication.dataset_version,
            "x-markethub-partial-completeness-revision": publication.partial_completeness_revision,
            "x-markethub-generation-pin": publication.generation_pin,
        }.items():
            if headers.get(header, "") != expected:
                raise MarketHubContractError(f"partial futures header {header} mismatch")
        required = (
            "qmi_id",
            "catalog_identity",
            "source_boundary_manifest",
            "source_manifests",
            "lineage_limitations",
        )
        if kind == "bars" and any(not meta.get(key) for key in required):
            raise MarketHubContractError("partial futures lineage is incomplete")
        items = response.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise MarketHubContractError(f"partial futures {kind} items are invalid")
        return meta, items

    @staticmethod
    def _validate_partial_rows(rows, product):
        for row in rows:
            if str(row.get("product_code", "")).casefold() != product.casefold():
                raise MarketHubContractError("partial futures returned an unrequested product")
            if str(row.get("series_type", "")) != "back_adjusted_continuous":
                raise MarketHubContractError("partial futures series type drifted")
            boundaries, sources = row.get("boundary_ids"), row.get("source_keys")
            if (
                not isinstance(boundaries, list)
                or not boundaries
                or not isinstance(sources, list)
                or not sources
            ):
                raise MarketHubContractError("partial futures row lacks source boundary lineage")

    def fetch_futures_contracts(
        self,
        product_codes: tuple[str, ...],
    ) -> tuple[FuturesContractCatalogIdentity, dict[str, dict[str, Any]]]:
        frozen = self._require_open()
        if not frozen.futures_contract_dataset_version:
            raise MarketHubContractError(
                "health response lacks future_contract_reference dataset version"
            )
        rows = self._request(
            "GET",
            "/api/futures/contracts",
            query={"codes": ",".join(product_codes)},
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MarketHubContractError("futures contracts response must be a list of objects")
        if not rows:
            raise MarketHubContractError("futures contracts response is empty")
        identity_fields = {
            (
                str(row.get("catalog_schema_version", "")),
                str(row.get("catalog_dataset_version", "")),
                str(row.get("snapshot_id", "")),
                str(row.get("content_checksum", "")),
            )
            for row in rows
        }
        if len(identity_fields) != 1 or not all(next(iter(identity_fields))):
            raise MarketHubContractError("futures contract catalog identity is missing or mixed")
        schema_version, dataset_version, snapshot_id, content_checksum = next(iter(identity_fields))
        if dataset_version != frozen.futures_contract_dataset_version:
            raise MarketHubContractError("futures contract catalog dataset version mismatch")
        if any(row.get("snapshot_complete") is not True for row in rows):
            raise MarketHubContractError("futures contract catalog snapshot is incomplete")
        requested = {item.casefold() for item in product_codes}
        by_product: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            product = str(row.get("product_code", "")).casefold()
            if product not in requested:
                raise MarketHubContractError(
                    "futures contract catalog returned an unrequested product"
                )
            by_product.setdefault(product, []).append(row)
        if set(by_product) != requested:
            raise MarketHubContractError(
                f"futures contract catalog lacks products: {sorted(requested - set(by_product))}"
            )
        specs: dict[str, dict[str, Any]] = {}
        for product, product_rows in by_product.items():
            try:
                signatures = {
                    (
                        str(row.get("exchange", "")),
                        str(row.get("tick_size", "")),
                        int(row.get("price_precision", -1)),
                        str(row.get("multiplier", "")),
                        str(row.get("currency", "")),
                    )
                    for row in product_rows
                }
            except (TypeError, ValueError) as exc:
                raise MarketHubContractError(
                    f"futures contract catalog lacks native specs for product {product!r}"
                ) from exc
            if len(signatures) != 1:
                raise MarketHubContractError(
                    f"futures contract catalog native specs differ within product {product!r}"
                )
            exchange, tick_size, price_precision, multiplier, currency = next(iter(signatures))
            if (
                not exchange
                or not tick_size
                or price_precision < 0
                or not multiplier
                or currency != "CNY"
            ):
                raise MarketHubContractError(
                    f"futures contract catalog lacks native specs for product {product!r}"
                )
            try:
                if Decimal(tick_size) <= 0 or Decimal(multiplier) <= 0:
                    raise ValueError
            except (DecimalException, ValueError) as exc:
                raise MarketHubContractError(
                    f"futures contract catalog has invalid native specs for product {product!r}"
                ) from exc
            specs[product] = {
                "currency": currency,
                "exchange": exchange,
                "multiplier": Decimal(multiplier),
                "price_precision": price_precision,
                "tick_size": Decimal(tick_size),
            }
        return (
            FuturesContractCatalogIdentity(
                schema_version=schema_version,
                dataset_version=dataset_version,
                snapshot_id=snapshot_id,
                content_checksum=content_checksum,
            ),
            specs,
        )

    def fetch_futures_coverage(self, *, series_type: str) -> tuple[dict[str, Any], ...]:
        rows = self._request(
            "GET",
            "/api/futures/coverage",
            query={"series_type": series_type},
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MarketHubContractError("futures coverage response must be a list of objects")
        identities = [str(row.get("product_code", "")).casefold() for row in rows]
        if not all(identities) or len(identities) != len(set(identities)):
            raise MarketHubContractError("futures coverage contains missing or duplicate products")
        return tuple(rows)

    def fetch_futures_1m(
        self,
        *,
        product_code: str,
        series_type: str,
        start_date: date,
        end_date: date,
    ) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for page in self._iter_futures_1m_pages(
            product_code=product_code,
            series_type=series_type,
            start_date=start_date,
            end_date=end_date,
            page_size=FUTURES_PAGE_SIZE,
        ):
            rows.extend(page)
        return tuple(rows)

    def _iter_futures_1m_pages(
        self,
        *,
        product_code: str,
        series_type: str,
        start_date: date,
        end_date: date,
        page_size: int,
    ):
        cursor_time = datetime.combine(start_date, datetime.min.time(), tzinfo=SHANGHAI)
        end_time = datetime.combine(end_date, datetime.max.time(), tzinfo=SHANGHAI)
        previous: datetime | None = None
        while cursor_time <= end_time:
            page = self._request(
                "GET",
                "/api/futures/quotes/1m",
                query={
                    "codes": product_code,
                    "series_type": series_type,
                    "start_time": cursor_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "limit": page_size,
                },
            )
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise MarketHubContractError("futures 1m response must be a list of objects")
            for row in page:
                timestamp = _parse_futures_time(row.get("bar_time"))
                if previous is not None and timestamp <= previous:
                    raise MarketHubContractError(
                        f"futures 1m ordering violation at {product_code}/{timestamp.isoformat()}"
                    )
                if str(row.get("product_code", "")).casefold() != product_code.casefold():
                    raise MarketHubContractError("futures 1m returned an unrequested product")
                previous = timestamp
            if page:
                yield tuple(page)
            if len(page) < page_size:
                break
            if previous is None:
                raise MarketHubContractError("futures 1m full page has no last timestamp")
            cursor_time = previous + timedelta(minutes=1)

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
            futures_1m_dataset_version=str(versions.get("future_bar_1m", "")),
            futures_contract_dataset_version=str(versions.get("future_contract_reference", "")),
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

    def _request_with_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        request = getattr(self._transport, "request_json_with_headers", None)
        if not callable(request):
            raise MarketHubContractError("partial futures transport must expose response headers")
        decoded, response_bytes, elapsed, headers = request(method, path, query=query, body=body)
        if not isinstance(headers, dict):
            raise MarketHubContractError("partial futures response headers are invalid")
        self.metrics.request_count += 1
        self.metrics.response_bytes += response_bytes
        self.metrics.fetch_seconds += elapsed
        return decoded, {str(key).casefold(): str(value) for key, value in headers.items()}

    def _require_open(self) -> HealthVector:
        if self._health is None:
            raise MarketHubContractError("MarketHub client is not open")
        return self._health


def _parse_futures_time(value: Any) -> datetime:
    if value is None or not str(value).strip():
        raise ValueError("futures bar_time is missing")
    rendered = str(value).strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ValueError(f"invalid futures bar_time {value!r}") from exc
    if result.tzinfo is None:
        return result.replace(tzinfo=SHANGHAI)
    return result.astimezone(SHANGHAI)


def _catalog_native_fields(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return {
        "currency": value["currency"],
        "multiplier": value["multiplier"],
        "price_precision": value["price_precision"],
        "tick_size": value["tick_size"],
    }


def _partial_identity(meta: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return tuple(
        str(meta.get(key, ""))
        for key in (
            "dataset_id",
            "dataset_version",
            "partial_completeness_revision",
            "generation_pin",
            "qmi_id",
            "catalog_identity",
        )
    )


def _require_partial_lineage(value: dict[str, Any] | None, key: str) -> Any:
    if value is None or not value.get(key):
        raise MarketHubContractError(f"partial futures lineage lacks {key}")
    return value[key]
