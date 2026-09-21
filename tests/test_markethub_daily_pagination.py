from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import Any

import pytest

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubContractError

INSTRUMENTS = ("SH.600000", "SZ.000001")


class ChainedDailyTransport:
    """Serve the fixture daily answers as a real cursor chain."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = deepcopy(fixture)
        self.cursors: list[str | None] = []

    def request_json(self, method, path, *, query=None, body=None):
        del query
        if method == "GET" and path == "/api/health":
            value = self.fixture["health"]
        elif method == "GET" and path == "/api/stocks/catalog":
            value = self.fixture["catalog"]
        elif method == "GET" and path == "/api/markets/calendar/trading":
            value = self.fixture["calendar"]
        elif method == "POST" and path == "/api/stocks/quotes/daily-window/query":
            cursor = (body or {}).get("cursor")
            self.cursors.append(cursor)
            value = self._answer(cursor)
        else:
            raise AssertionError(f"unexpected request {method} {path}")
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001

    def _answer(self, cursor: str | None) -> dict[str, Any]:
        pages = self.fixture["daily_pages"]
        if cursor is None:
            return pages[0]
        for index, page in enumerate(pages[:-1]):
            if page["meta"]["next_cursor"] == cursor:
                return pages[index + 1]
        raise AssertionError(f"unexpected cursor {cursor!r}")


def paged_fixture(market_fixture: dict[str, Any], *sizes: int) -> dict[str, Any]:
    """Re-cut the fixture delivery into `sizes` answers with MarketHub delivery semantics."""
    fixture = deepcopy(market_fixture)
    items = [row for page in fixture["daily_pages"] for row in page["items"]]
    template = fixture["daily_pages"][0]["meta"]
    pages = []
    offset = 0
    for index, size in enumerate(sizes):
        final = index == len(sizes) - 1
        meta = deepcopy(template)
        meta.update(
            returned_rows=size,
            total_rows=len(items),
            next_cursor=None if final else f"page-{index + 2}",
            delivery_complete=final,
        )
        pages.append({"items": items[offset : offset + size], "meta": meta})
        offset += size
    fixture["daily_pages"] = pages
    return fixture


def fetch(fixture: dict[str, Any]) -> tuple[Any, ChainedDailyTransport]:
    transport = ChainedDailyTransport(fixture)
    client = MarketHubClient(transport=transport)
    dataset = client.fetch_dataset(INSTRUMENTS, date(2025, 1, 1), date(2025, 1, 31), page_size=2)
    return dataset, transport


def test_middle_answers_may_report_an_incomplete_delivery(market_fixture: dict) -> None:
    dataset, transport = fetch(paged_fixture(market_fixture, 2, 2, 2))

    assert transport.cursors == [None, "page-2", "page-3"]
    assert len(dataset.bars) == 6


def test_single_answer_delivery_behaviour_is_unchanged(market_fixture: dict) -> None:
    dataset, transport = fetch(paged_fixture(market_fixture, 6))

    assert transport.cursors == [None]
    assert len(dataset.bars) == 6

    broken = paged_fixture(market_fixture, 6)
    broken["daily_pages"][0]["meta"]["delivery_complete"] = False
    with pytest.raises(MarketHubContractError, match="delivery_complete is not true"):
        fetch(broken)


def test_final_answer_must_report_a_complete_delivery(market_fixture: dict) -> None:
    broken = paged_fixture(market_fixture, 2, 2, 2)
    broken["daily_pages"][-1]["meta"]["delivery_complete"] = False

    with pytest.raises(MarketHubContractError, match="delivery_complete is not true"):
        fetch(broken)


def test_every_answer_must_declare_the_delivery_state(market_fixture: dict) -> None:
    broken = paged_fixture(market_fixture, 2, 2, 2)
    del broken["daily_pages"][0]["meta"]["delivery_complete"]

    with pytest.raises(MarketHubContractError, match="delivery_complete is missing"):
        fetch(broken)


def test_repeated_cursor_breaks_the_chain(market_fixture: dict) -> None:
    broken = paged_fixture(market_fixture, 2, 2, 2)
    broken["daily_pages"][1]["meta"]["next_cursor"] = "page-2"

    with pytest.raises(MarketHubContractError, match="cursor is invalid or repeated"):
        fetch(broken)


def test_delivery_without_its_final_answer_is_rejected(market_fixture: dict) -> None:
    truncated = paged_fixture(market_fixture, 2, 2, 0)

    with pytest.raises(MarketHubContractError, match="delivered 4 rows but declared 6"):
        fetch(truncated)


@pytest.mark.parametrize(
    ("field", "drifted", "match"),
    (
        ("data_version", "fixture-global-v2", "data_version mismatch"),
        ("dataset_version", "fixture-daily-v2", "dataset_version mismatch"),
    ),
)
def test_version_drift_between_answers_is_rejected(
    market_fixture: dict, field: str, drifted: str, match: str
) -> None:
    broken = paged_fixture(market_fixture, 2, 2, 2)
    broken["daily_pages"][1]["meta"][field] = drifted

    with pytest.raises(MarketHubContractError, match=match):
        fetch(broken)


def test_answers_must_stay_ordered_across_the_chain(market_fixture: dict) -> None:
    broken = paged_fixture(market_fixture, 2, 2, 2)
    broken["daily_pages"][1]["items"].reverse()

    with pytest.raises(MarketHubContractError, match="ordering violation"):
        fetch(broken)
