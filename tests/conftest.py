from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from quant_runtime.markethub.client import MarketHubClient


class FixtureTransport:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = deepcopy(fixture)
        self.daily_page_index = 0
        self.health_reads = 0

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, int, float]:
        del query
        if method == "GET" and path == "/api/health":
            self.health_reads += 1
            value = self.fixture["health"]
        elif method == "GET" and path == "/api/stocks/catalog":
            value = self.fixture["catalog"]
        elif method == "GET" and path == "/api/markets/calendar/trading":
            value = self.fixture["calendar"]
        elif method == "POST" and path == "/api/stocks/quotes/daily-window/query":
            expected = None if self.daily_page_index == 0 else "opaque-page-2"
            assert (body or {}).get("cursor") == expected
            value = self.fixture["daily_pages"][self.daily_page_index]
            self.daily_page_index += 1
        else:
            raise AssertionError(f"unexpected request {method} {path}")
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001


@pytest.fixture
def s_fixture() -> dict[str, Any]:
    days = [
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
        "2025-01-07",
        "2025-01-08",
        "2025-01-09",
        "2025-01-10",
    ]
    prices = {
        "000001": ["10.1", "10.3", "10.5", "10.6", "10.8", "10.9", "11.1"],
        "600000": ["19.8", "19.9", "19.5", "19.8", "19.3", "19.7", "19.1"],
    }
    items = []
    for index, trading_day in enumerate(days):
        for code in ("000001", "600000"):
            close = Decimal(prices[code][index])
            pre_close = Decimal(prices[code][index - 1]) if index else close
            items.append(
                {
                    "trade_time": trading_day,
                    "code": code,
                    "open": str(close),
                    "high": str(close + Decimal("0.2")),
                    "low": str(close - Decimal("0.2")),
                    "close": str(close),
                    "volume": "1000",
                    "amount": str(close * 1000),
                    "pre_close": str(pre_close),
                    "is_suspended": False,
                    "is_st": False,
                }
            )

    def meta(page_size: int, cursor: str | None) -> dict[str, Any]:
        return {
            "data_version": "fixture-global-v1",
            "dataset_version": "fixture-daily-v1",
            "universe_kind": "codes",
            "truncated": False,
            "complete": True,
            "page_complete": True,
            "request_complete": True,
            "delivery_complete": True,
            "returned_rows": page_size,
            "total_rows": len(items),
            "next_cursor": cursor,
            "coverage": [
                {
                    "code": code,
                    "complete": True,
                    "missing_rows": 0,
                    "expected_rows": 7,
                    "actual_rows": 7,
                }
                for code in ("000001", "600000")
            ],
        }

    return {
        "health": {
            "status": "ok",
            "data_version": "fixture-global-v1",
            "dataset_versions": {"stock_daily_1d": "fixture-daily-v1"},
        },
        "catalog": [
            {"code": "000001", "exchange": "SZSE", "list_date": "1991-04-03"},
            {"code": "600000", "exchange": "SHSE", "list_date": "1999-11-10"},
        ],
        "calendar": [{"trade_date": value, "is_open": True} for value in days],
        "daily_pages": [
            {"items": items[:8], "meta": meta(8, "opaque-page-2")},
            {"items": items[8:], "meta": meta(6, None)},
        ],
    }


@pytest.fixture
def fixture_client(s_fixture: dict[str, Any]) -> MarketHubClient:
    return MarketHubClient(transport=FixtureTransport(s_fixture))


@pytest.fixture
def canonical_dataset(fixture_client: MarketHubClient):
    return fixture_client.fetch_dataset(
        ("SH.600000", "SZ.000001"),
        date(2025, 1, 1),
        date(2025, 1, 31),
        page_size=8,
    )
