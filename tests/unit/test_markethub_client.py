from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from conftest import FixtureTransport

from quant_runtime.discovery.qlib.qlib_loader import load_frame
from quant_runtime.market_data.markethub.client import MarketHubClient, MarketHubContractError


def test_shared_dataset_is_canonical_and_qlib_ready(canonical_dataset) -> None:
    assert canonical_dataset.data_version == "fixture-global-v1"
    assert canonical_dataset.dataset_version == "fixture-daily-v1"
    assert len(canonical_dataset.bars) == 14
    assert len(canonical_dataset.input_hash) == 64
    frame = load_frame(canonical_dataset)
    assert frame.index.names == ["datetime", "instrument"]
    assert frame.index.is_monotonic_increasing


def test_rejects_out_of_order_daily_rows(s_fixture: dict) -> None:
    broken = deepcopy(s_fixture)
    broken["daily_pages"][0]["items"][0], broken["daily_pages"][0]["items"][1] = (
        broken["daily_pages"][0]["items"][1],
        broken["daily_pages"][0]["items"][0],
    )
    client = MarketHubClient(transport=FixtureTransport(broken))
    client.open()
    with pytest.raises(MarketHubContractError, match="ordering violation"):
        client.fetch_daily(
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
            codes=("000001", "600000"),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            page_size=8,
        )


def test_rejects_incomplete_coverage(s_fixture: dict) -> None:
    broken = deepcopy(s_fixture)
    broken["daily_pages"][0]["meta"]["coverage"][0].update(
        complete=False, missing_rows=1, actual_rows=6
    )
    client = MarketHubClient(transport=FixtureTransport(broken))
    client.open()
    with pytest.raises(MarketHubContractError, match="coverage is incomplete"):
        client.fetch_daily(
            codes=("000001", "600000"),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            page_size=8,
        )


def test_rejects_both_global_and_dataset_version_drift(s_fixture: dict) -> None:
    class DriftTransport(FixtureTransport):
        def request_json(self, *args, **kwargs):
            value, size, elapsed = super().request_json(*args, **kwargs)
            if args[1] == "/api/health" and self.health_reads == 2:
                value["dataset_versions"]["stock_daily_1d"] = "fixture-daily-v2"
            return value, size, elapsed

    client = MarketHubClient(transport=DriftTransport(s_fixture))
    client.open()
    with pytest.raises(MarketHubContractError, match="version drift"):
        client.verify_version()


def test_suspended_null_prices_use_preclose_only_in_canonical_memory(s_fixture: dict) -> None:
    row = s_fixture["daily_pages"][0]["items"][0]
    row.update(is_suspended=True, open=None, high=None, low=None, close=None)
    dataset = MarketHubClient(transport=FixtureTransport(s_fixture)).fetch_dataset(
        ("SH.600000", "SZ.000001"),
        date(2025, 1, 1),
        date(2025, 1, 31),
        page_size=8,
    )
    item = next(bar for bar in dataset.bars if bar.identity == (date(2025, 1, 2), "SZ.000001"))
    assert item.is_suspended
    assert item.open == item.high == item.low == item.close == item.pre_close
