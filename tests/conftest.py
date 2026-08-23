from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from markethub_qlib.client import MarketHubClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "s_canonical_contract.json"


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
            expected_cursor = None if self.daily_page_index == 0 else "opaque-page-2"
            assert (body or {}).get("cursor") == expected_cursor
            value = self.fixture["daily_pages"][self.daily_page_index]
            self.daily_page_index += 1
        else:
            raise AssertionError(f"unexpected request {method} {path}")
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001


@pytest.fixture
def s_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_client(s_fixture: dict[str, Any]) -> MarketHubClient:
    return MarketHubClient(transport=FixtureTransport(s_fixture))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-connected",
        action="store_true",
        default=False,
        help="run tests requiring live yosef-server MarketHub",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-connected"):
        return
    skip_connected = pytest.mark.skip(reason="requires --run-connected")
    for item in items:
        if "connected" in item.keywords:
            item.add_marker(skip_connected)
