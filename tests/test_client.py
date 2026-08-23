from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from conftest import FixtureTransport

from markethub_qlib.client import MarketHubClient, MarketHubContractError

FIELDS = ("open", "high", "low", "close", "volume", "amount", "pre_close")


def test_loads_paginated_qlib_multiindex(fixture_client: MarketHubClient) -> None:
    dataset = fixture_client.load_qlib_frame(
        universe_kind="codes",
        codes=("000001", "600000"),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        fields=FIELDS,
        page_size=8,
    )
    assert dataset.frame.index.names == ["datetime", "instrument"]
    assert dataset.frame.index.is_monotonic_increasing
    assert len(dataset.frame) == 14
    assert dataset.instruments == ("SH.600000", "SZ.000001")
    assert len(dataset.canonical_input_hash) == 64


def test_rejects_out_of_order_daily_rows(s_fixture: dict) -> None:
    broken = deepcopy(s_fixture)
    broken["daily_pages"][0]["items"][0], broken["daily_pages"][0]["items"][1] = (
        broken["daily_pages"][0]["items"][1],
        broken["daily_pages"][0]["items"][0],
    )
    client = MarketHubClient(transport=FixtureTransport(broken))
    client.open()
    client.fetch_catalog()
    client.fetch_calendar(date(2025, 1, 1), date(2025, 1, 31))
    with pytest.raises(MarketHubContractError, match="out of order"):
        client.fetch_daily(
            universe_kind="codes",
            codes=("000001", "600000"),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            page_size=8,
        )


@pytest.mark.parametrize("field,value", [("complete", False), ("truncated", True)])
def test_rejects_incomplete_or_truncated_delivery(s_fixture: dict, field: str, value: bool) -> None:
    broken = deepcopy(s_fixture)
    broken["daily_pages"][0]["meta"][field] = value
    client = MarketHubClient(transport=FixtureTransport(broken))
    client.open()
    with pytest.raises(MarketHubContractError, match=field):
        client.fetch_daily(
            universe_kind="codes",
            codes=("000001", "600000"),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            page_size=8,
        )


def test_rejects_incomplete_coverage(s_fixture: dict) -> None:
    broken = deepcopy(s_fixture)
    coverage = broken["daily_pages"][0]["meta"]["coverage"][0]
    coverage.update(complete=False, missing_rows=1, actual_rows=6)
    client = MarketHubClient(transport=FixtureTransport(broken))
    client.open()
    with pytest.raises(MarketHubContractError, match="coverage is incomplete"):
        client.fetch_daily(
            universe_kind="codes",
            codes=("000001", "600000"),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            page_size=8,
        )


def test_rejects_version_drift(s_fixture: dict) -> None:
    class DriftTransport(FixtureTransport):
        def request_json(self, *args, **kwargs):
            value, size, elapsed = super().request_json(*args, **kwargs)
            if args[1] == "/api/health" and self.health_reads == 2:
                value["data_version"] = "fixture-global-v2"
            return value, size, elapsed

    client = MarketHubClient(transport=DriftTransport(s_fixture))
    client.open()
    with pytest.raises(MarketHubContractError, match="version drift"):
        client.verify_version()


def test_suspended_null_prices_are_preserved_but_flagged(s_fixture: dict) -> None:
    suspended = s_fixture["daily_pages"][0]["items"][0]
    suspended.update(is_suspended=True, open=None, high=None, low=None, close=None)
    client = MarketHubClient(transport=FixtureTransport(s_fixture))
    dataset = client.load_qlib_frame(
        universe_kind="codes",
        codes=("000001", "600000"),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        fields=FIELDS,
        page_size=8,
    )
    row = dataset.frame.loc[("2025-01-02", "SZ.000001")]
    assert bool(row["is_suspended"])
    assert row[["open", "high", "low", "close"]].isna().all()
