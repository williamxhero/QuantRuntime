from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_runtime.adapters.data.markethub import MarketHubClient

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "tests" / "fixtures" / "noop-strategy"


class FixtureTransport:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = deepcopy(fixture)
        self.daily_page_index = 0
        self.health_reads = 0

    def request_json(self, method, path, *, query=None, body=None):
        del query
        if method == "GET" and path == "/api/health":
            self.health_reads += 1
            value = self.fixture["health"]
        elif method == "GET" and path == "/api/stocks/catalog":
            value = self.fixture["catalog"]
        elif method == "GET" and path == "/api/markets/calendar/trading":
            value = self.fixture["calendar"]
        elif method == "POST" and path == "/api/stocks/quotes/daily-window/query":
            expected = None if self.daily_page_index == 0 else "page-2"
            assert (body or {}).get("cursor") == expected
            value = self.fixture["daily_pages"][self.daily_page_index]
            self.daily_page_index += 1
        else:
            raise AssertionError(f"unexpected request {method} {path}")
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001


@pytest.fixture
def market_fixture() -> dict[str, Any]:
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    prices = {"000001": ["10.1", "10.3", "10.5"], "600000": ["19.8", "19.9", "19.5"]}
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

    def meta(size: int, cursor: str | None) -> dict[str, Any]:
        return {
            "data_version": "fixture-global-v1",
            "dataset_version": "fixture-daily-v1",
            "universe_kind": "codes",
            "truncated": False,
            "complete": True,
            "page_complete": True,
            "request_complete": True,
            "delivery_complete": True,
            "returned_rows": size,
            "total_rows": len(items),
            "next_cursor": cursor,
            "coverage": [
                {
                    "code": code,
                    "complete": True,
                    "missing_rows": 0,
                    "expected_rows": 3,
                    "actual_rows": 3,
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
            {"items": items[:4], "meta": meta(4, "page-2")},
            {"items": items[4:], "meta": meta(2, None)},
        ],
    }


@pytest.fixture
def fixture_client(market_fixture: dict[str, Any]) -> MarketHubClient:
    return MarketHubClient(transport=FixtureTransport(market_fixture))


@pytest.fixture
def canonical_dataset(fixture_client: MarketHubClient):
    return fixture_client.fetch_dataset(
        ("SH.600000", "SZ.000001"), date(2025, 1, 1), date(2025, 1, 31), page_size=4
    )
