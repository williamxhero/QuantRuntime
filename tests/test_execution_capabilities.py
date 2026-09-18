from __future__ import annotations

from typing import Any

import pytest
from conftest import FixtureTransport

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubDataAdapter
from quant_runtime.execution_capabilities import (
    REQUIRED_FIELDS,
    SCHEMA,
    ExecutionPreflightError,
    run_execution_preflight,
)


def request_value() -> dict[str, Any]:
    return {
        "schema": "quant-runtime.execution-preflight-request.v1",
        "market": {
            "asset_class": "equity",
            "exchanges": ["SHSE", "SZSE", "BJSE"],
            "frequency": "1d",
            "direction": "long-only",
        },
        "range": {
            "start": "2025-01-01",
            "end": "2025-01-31",
            "windows": [
                {
                    "label": "development",
                    "start": "2025-01-01",
                    "end": "2025-01-31",
                    "instruments": ["SH.600000", "SZ.000001"],
                }
            ],
        },
        "probe": {
            "base_url": "http://fixture",
            "endpoint_contract": "v2",
            "calendar": "cn-equity-v1",
            "as_of": "2025-02-01T00:00:00Z",
            "adjustment": "none",
            "required_semantics": ["field_availability", "time"],
        },
    }


class BlockedDailyTransport(FixtureTransport):
    def request_json(self, method, path, *, query=None, body=None):
        if path == "/api/stocks/quotes/daily-window/query":
            raise AssertionError("the daily read must not be reached")
        return super().request_json(method, path, query=query, body=body)


def snapshot(market_fixture: dict[str, Any], transport_class: type = FixtureTransport):
    def factory(_request):
        return MarketHubClient(transport=transport_class(market_fixture))

    return run_execution_preflight(
        request_value(),
        adapter=MarketHubDataAdapter(client_factory=factory),
        client=MarketHubClient(transport=transport_class(market_fixture)),
    )


def test_execution_preflight_covers_every_required_capability(
    market_fixture: dict[str, Any],
) -> None:
    result = snapshot(market_fixture)

    assert result["schema"] == SCHEMA
    assert result["status"] == "evaluated"
    assert result["snapshot_id"].startswith("sha256:")
    assert {item["capability"] for item in result["capabilities"]} == set(REQUIRED_FIELDS)
    assert result["source"]["reads_only"] is True


def test_every_capability_cites_a_source_and_explains_itself(
    market_fixture: dict[str, Any],
) -> None:
    for item in snapshot(market_fixture)["capabilities"]:
        assert item["sources"], item["capability"]
        assert all(
            source["kind"] in {"runtime_code", "markethub_live", "markethub_unavailable"}
            for source in item["sources"]
        )
        if item["status"] == "not_evaluated":
            assert item["reason"]
        if item["status"] == "supported_with_limitation":
            assert item["limitation"]


def test_a_blocked_live_read_degrades_instead_of_asserting_support(
    market_fixture: dict[str, Any],
) -> None:
    result = snapshot(market_fixture, transport_class=BlockedDailyTransport)

    assert result["range"]["bar_evidence"] == "unavailable"
    assert all(window["status"] == "unavailable" for window in result["range"]["windows"])
    assert all(window["error"] for window in result["range"]["windows"])
    blocked = {
        item["capability"]
        for item in result["capabilities"]
        for source in item["sources"]
        if source["kind"] == "markethub_unavailable"
    }
    assert "suspension" in blocked and "limit_up_down" in blocked


def test_the_request_refuses_an_unreal_source_or_market() -> None:
    fixture_free = request_value()
    fixture_free["probe"]["base_url"] = "fixture://local"
    with pytest.raises(ExecutionPreflightError):
        run_execution_preflight(fixture_free)

    wrong_frequency = request_value()
    wrong_frequency["market"]["frequency"] = "1m"
    with pytest.raises(ExecutionPreflightError):
        run_execution_preflight(wrong_frequency)


def test_a_window_cannot_escape_the_declared_range() -> None:
    escaping = request_value()
    escaping["range"]["windows"][0]["end"] = "2025-03-31"
    with pytest.raises(ExecutionPreflightError):
        run_execution_preflight(escaping)
