from collections import deque
from datetime import date
from typing import Any

import pytest

from markethub_nautilus.canonical import CanonicalDataError
from markethub_nautilus.markethub import MarketHubClient, MarketHubError


class FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)

    def request_json(self, method, path, *, query=None, body=None):
        return self.responses.popleft(), 100, 0.01, 0.001


def test_version_drift_fails_closed() -> None:
    client = MarketHubClient(
        transport=FakeTransport(
            [
                {"status": "ok", "data_version": "v1"},
                {"status": "ok", "data_version": "v2"},
            ]
        )
    )
    client.open()
    with pytest.raises(MarketHubError, match="data_version drift"):
        client.verify_version()


def test_calendar_order_fails_closed() -> None:
    client = MarketHubClient(
        transport=FakeTransport(
            [
                {"status": "ok", "data_version": "v1"},
                [{"trade_date": "2025-01-03"}, {"trade_date": "2025-01-02"}],
            ]
        )
    )
    client.open()
    with pytest.raises(CanonicalDataError, match="calendar"):
        client.fetch_calendar(date(2025, 1, 1), date(2025, 1, 31))


def test_daily_completeness_and_order_fail_closed() -> None:
    instrument_page = [{"code": "600000", "exchange": "SHSE", "list_date": "2000-01-01"}]
    client = MarketHubClient(
        transport=FakeTransport(
            [
                {"status": "ok", "data_version": "v1"},
                instrument_page,
                [{"trade_date": "2025-01-02"}],
                {"items": [], "meta": {"data_version": "v1", "complete": False}},
            ]
        )
    )
    with pytest.raises(MarketHubError, match="complete"):
        client.fetch_dataset(("SH.600000",), date(2025, 1, 1), date(2025, 1, 2))


def test_daily_empty_in_range_instrument_fails_coverage() -> None:
    instrument_page = [{"code": "600000", "exchange": "SHSE", "list_date": "2000-01-01"}]
    client = MarketHubClient(
        transport=FakeTransport(
            [
                {"status": "ok", "data_version": "v1"},
                instrument_page,
                [{"trade_date": "2025-01-02"}],
                {
                    "items": [],
                    "meta": {
                        "data_version": "v1",
                        "complete": True,
                        "request_complete": True,
                        "delivery_complete": True,
                        "next_cursor": None,
                    },
                },
            ]
        )
    )
    with pytest.raises(CanonicalDataError, match="no rows"):
        client.fetch_dataset(("SH.600000",), date(2025, 1, 1), date(2025, 1, 2))
