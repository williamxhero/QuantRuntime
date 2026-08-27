from __future__ import annotations

from datetime import date

import httpx
import pytest

from quant_runtime.adapters.data.markethub.client import (
    HttpxJsonTransport,
    MarketHubClient,
    MarketHubContractError,
)
from quant_runtime.adapters.data.markethub.contract import PartialFuturesPublication

PUBLICATION = PartialFuturesPublication(
    dataset_id="future_1m_partial_s000012_quotemux",
    dataset_version="qmp-v1-fixture",
    partial_completeness_revision="qmc-v1-fixture",
    generation_pin="qmg-v1-fixture",
)


def _coverage_response():
    return {
        "items": [],
        "meta": {
            **PUBLICATION.as_dict(),
            "catalog_identity": "qmf-catalog-v1-fixture",
            "missing_bar_semantics": "skip",
            "partial_contract_satisfied": True,
            "coverage_semantics": "observed_admitted_runs_only",
            "residual_semantics": "excluded_or_missing_rows_are_skipped",
            "warmup": {},
            "next_cursor": None,
        },
    }


class RecordingTransport:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.queries: list[dict] = []

    def request_json_with_headers(self, method, path, *, query=None, body=None):
        del method, path, body
        assert query is not None
        self.queries.append(dict(query))
        if self.failures:
            raise self.failures.pop(0)
        return (
            _coverage_response(),
            1,
            0.001,
            {
                "x-markethub-dataset-version": PUBLICATION.dataset_version,
                "x-markethub-partial-completeness-revision": (
                    PUBLICATION.partial_completeness_revision
                ),
                "x-markethub-generation-pin": PUBLICATION.generation_pin,
            },
        )


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://fixture/page")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("status", request=request, response=response)


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), _status_error(503)],
)
def test_retryable_transport_failures_retry_three_times_with_fixed_backoff(failure) -> None:
    transport = RecordingTransport([failure, failure, failure])
    waits: list[float] = []
    client = MarketHubClient(transport=transport, sleeper=waits.append)

    client._request_with_headers("GET", "/page", query={"cursor": "fixed"})

    assert [item["cursor"] for item in transport.queries] == ["fixed"] * 4
    assert waits == [1, 2, 4]


@pytest.mark.parametrize(
    "failure", [_status_error(400), httpx.WriteTimeout("write"), ValueError("bad")]
)
def test_non_retryable_transport_or_protocol_failures_fail_closed(failure) -> None:
    transport = RecordingTransport([failure])
    client = MarketHubClient(transport=transport, sleeper=lambda _: pytest.fail("slept"))

    with pytest.raises(MarketHubContractError):
        client._request_with_headers("GET", "/page", query={"cursor": "fixed"})

    assert len(transport.queries) == 1


def test_partial_page_reduces_only_after_consecutive_retryable_failures() -> None:
    transport = RecordingTransport([httpx.ConnectError("offline")] * 4)
    waits: list[float] = []
    client = MarketHubClient(transport=transport, sleeper=waits.append)

    pages = list(
        client._iter_partial_pages(
            PUBLICATION,
            "/api/futures/quotes/1m/partial/coverage",
            ("ag",),
            date(2025, 1, 1),
            date(2025, 1, 2),
            "coverage",
        )
    )

    assert pages and pages[0][0] == ()
    assert [item["limit"] for item in transport.queries] == [10_000] * 4 + [5_000]
    assert waits == [1, 2, 4]


def test_partial_page_reduction_stops_at_minimum_and_fails_closed() -> None:
    transport = RecordingTransport([httpx.ConnectError("offline")] * 20)
    client = MarketHubClient(transport=transport, sleeper=lambda _: None)

    with pytest.raises(MarketHubContractError, match="failed after 3 retries"):
        list(
            client._iter_partial_pages(
                PUBLICATION,
                "/api/futures/quotes/1m/partial/coverage",
                ("ag",),
                date(2025, 1, 1),
                date(2025, 1, 2),
                "coverage",
            )
        )

    assert [item["limit"] for item in transport.queries] == (
        [10_000] * 4 + [5_000] * 4 + [2_500] * 4 + [1_250] * 4 + [1_000] * 4
    )


def test_httpx_transport_uses_fixed_component_timeouts(monkeypatch) -> None:
    def fake_request(*args, **kwargs):
        timeout = kwargs["timeout"]
        assert timeout.connect == 10.0
        assert timeout.read == 120.0
        assert timeout.write == 30.0
        assert timeout.pool == 30.0
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(httpx.ConnectError):
        HttpxJsonTransport("http://fixture").request_json("GET", "/page")
