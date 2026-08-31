from __future__ import annotations

import base64
import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from strategy_workspace import WorkspaceClient, WorkspaceWorker

from quant_runtime.adapters.data.markethub import (
    MarketHubClient,
    MarketHubContractError,
    MarketHubDataAdapter,
)
from quant_runtime.adapters.data.markethub.contract import SnapshotRequest
from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesBars,
    CanonicalFuturesDataset,
    CanonicalFuturesInstrument,
)
from quant_runtime.adapters.data.markethub.storage import AdapterStorage
from quant_runtime.adapters.formal.nautilus import FuturesExecutionConfig, FuturesStrategyContext
from quant_runtime.adapters.formal.nautilus.futures_runner import (
    _signal_batches,
    run_futures_engine,
)
from quant_runtime.adapters.formal.nautilus.runner import StrategyContext
from quant_runtime.entrypoint import load_package_entrypoint
from quant_runtime.executor import RuntimeExecutor
from quant_runtime.registry import production_registry

PACKAGE = Path(__file__).parent / "fixtures" / "futures-native"


class FuturesTransport:
    def __init__(
        self,
        *,
        missing_offset: bool = False,
        drift_unrelated_global_version: bool = False,
        missing_catalog_tick: bool = False,
    ) -> None:
        self.missing_offset = missing_offset
        self.drift_unrelated_global_version = drift_unrelated_global_version
        self.missing_catalog_tick = missing_catalog_tick
        self.health_reads = 0

    def request_json(self, method, path, *, query=None, body=None):
        del body
        if method == "GET" and path == "/api/health":
            self.health_reads += 1
            value = {
                "status": "ok",
                "data_version": (
                    f"fixture-global-v{self.health_reads}"
                    if self.drift_unrelated_global_version
                    else "fixture-global-v1"
                ),
                "dataset_versions": {
                    "future_bar_1m": "fixture-futures-v1",
                    "future_contract_reference": "fixture-contracts-v1",
                    "stock_daily_1d": "fixture-daily-v1",
                },
            }
        elif method == "GET" and path == "/api/futures/contracts":
            assert query == {"codes": "ag"}
            value = [
                {
                    "product_code": "ag",
                    "exchange": "SHFE",
                    "tick_size": None if self.missing_catalog_tick else "1",
                    "price_precision": 0,
                    "multiplier": "15",
                    "currency": "CNY",
                    "catalog_schema_version": "future_contract_catalog_v2",
                    "catalog_dataset_version": "fixture-contracts-v1",
                    "snapshot_id": "fixture-contract-snapshot",
                    "snapshot_complete": True,
                    "content_checksum": "d" * 64,
                }
            ]
        elif method == "GET" and path == "/api/futures/coverage":
            value = [
                {
                    "product_code": "ag",
                    "exchange": "SHFE",
                    "series_type": "back_adjusted_continuous",
                    "row_count": 3,
                    "first_bar_time": "2025-01-02 09:01:00",
                    "last_bar_time": "2025-01-02 09:03:00",
                }
            ]
        elif method == "GET" and path == "/api/futures/quotes/1m":
            assert query == {
                "codes": "ag",
                "series_type": "back_adjusted_continuous",
                "start_time": "2025-01-02 00:00:00",
                "end_time": "2025-01-02 23:59:59",
                "limit": 200000,
            }
            value = []
            for minute, close in enumerate(("100", "101", "102"), start=1):
                value.append(
                    {
                        "product_code": "ag",
                        "exchange": "SHFE",
                        "series_type": "back_adjusted_continuous",
                        "bar_time": f"2025-01-02 09:0{minute}:00",
                        "open": close,
                        "high": str(int(close) + 1),
                        "low": str(int(close) - 1),
                        "close": close,
                        "volume": "100",
                        "open_interest": None,
                        "adjustment_offset": None if self.missing_offset else "10",
                    }
                )
        else:
            raise AssertionError(f"unexpected request {method} {path}")
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001


class PartialFuturesTransport:
    publication = {
        "dataset_id": "future_1m_partial_s000012_quotemux",
        "dataset_version": "qmp-v1-fixture",
        "partial_completeness_revision": "qmc-v1-fixture",
        "generation_pin": "qmg-v1-fixture",
    }

    def __init__(
        self,
        *,
        repeated_cursor: bool = False,
        paged: bool = False,
        empty_cursor: bool = False,
        lineage_drift_after_scan: bool = False,
        coverage_count_delta: int = 0,
        coverage_catalog_drift: bool = False,
        coverage_semantics_drift: bool = False,
        missing_bars_lineage: bool = False,
    ) -> None:
        self.paths: list[str] = []
        self.repeated_cursor = repeated_cursor
        self.paged = paged
        self.empty_cursor = empty_cursor
        self.lineage_drift_after_scan = lineage_drift_after_scan
        self.coverage_count_delta = coverage_count_delta
        self.coverage_catalog_drift = coverage_catalog_drift
        self.coverage_semantics_drift = coverage_semantics_drift
        self.missing_bars_lineage = missing_bars_lineage
        self.bar_reads = 0
        self.coverage_reads = 0

    def request_json_with_headers(self, method, path, *, query=None, body=None):
        del body
        self.paths.append(path)
        assert method == "GET"
        assert query is not None
        assert {key: query[key] for key in self.publication} == self.publication
        assert query["codes"] == "ag"
        meta = {
            **self.publication,
            "catalog_identity": "qmf-catalog-v1-fixture",
            "missing_bar_semantics": "skip",
            "warmup": {"residual_semantics": "excluded_or_missing_rows_are_skipped"},
            "partial_contract_satisfied": True,
            "next_cursor": None,
        }
        if path.endswith("/coverage"):
            self.coverage_reads += 1
            meta.update(
                {
                    "coverage_semantics": "observed_admitted_runs_only",
                    "residual_semantics": "excluded_or_missing_rows_are_skipped",
                }
            )
            if self.coverage_catalog_drift and self.coverage_reads > 1:
                meta["catalog_identity"] = "qmf-catalog-v1-drifted"
            if self.coverage_semantics_drift and self.coverage_reads > 1:
                meta["coverage_semantics"] = "drifted"
            if self.paged and "cursor" not in query:
                assert "cursor" not in query
                meta["next_cursor"] = "coverage-page-2"
                observed_count = 2
            elif self.paged:
                assert query["cursor"] == "coverage-page-2"
                observed_count = 1
            else:
                observed_count = 3 + self.coverage_count_delta
            value = {
                "items": [
                    {
                        "product_code": "ag",
                        "exchange": "SHFE",
                        "start_time": "2025-01-02 09:01:00",
                        "end_time": "2025-01-02 09:03:00",
                        "status": "accepted",
                        "observed_count": observed_count,
                    }
                ],
                "meta": meta,
            }
        elif path.endswith("/partial"):
            self.bar_reads += 1
            meta.update(
                {
                    "qmi_id": "qmi-v1-fixture",
                    "source_boundary_manifest": {"count": 1, "sha256": "a" * 64},
                    "source_manifests": [{"source_key": "fixture", "lineage": "observed"}],
                    "lineage_limitations": "fixture partial data is not session complete",
                    "session_grid": "not_asserted_complete",
                    "coverage": {
                        "endpoint": "/api/futures/quotes/1m/partial/coverage",
                        "semantics": "observed_admitted_runs_only",
                        "residual_semantics": "excluded_or_missing_rows_are_skipped",
                    },
                }
            )
            if self.missing_bars_lineage:
                del meta["source_manifests"]
            if self.lineage_drift_after_scan and self.bar_reads > (2 if self.paged else 1):
                meta["qmi_id"] = "qmi-v1-drifted"
            if self.paged and "cursor" not in query:
                assert "cursor" not in query
                meta["next_cursor"] = "bars-page-2"
                observed = ((1, "100"), (2, "101"))
            elif self.paged:
                assert query["cursor"] == "bars-page-2"
                observed = ((3, "102"),)
            else:
                observed = tuple(enumerate(("100", "101", "102"), start=1))
            value = {
                "items": (
                    []
                    if self.repeated_cursor and self.bar_reads > 1
                    else [
                        {
                            "product_code": "ag",
                            "exchange": "SHFE",
                            "series_type": "back_adjusted_continuous",
                            "bar_time": f"2025-01-02 09:0{minute}:00",
                            "open": close,
                            "high": str(int(close) + 1),
                            "low": str(int(close) - 1),
                            "close": close,
                            "volume": "100",
                            "open_interest": None,
                            "adjustment_offset": "10",
                            "boundary_ids": ["qmb-v1-fixture"],
                            "source_keys": ["fixture"],
                        }
                        for minute, close in observed
                    ]
                ),
                "meta": meta,
            }
            if self.repeated_cursor:
                value["meta"]["next_cursor"] = "fixture-repeated-cursor"
            if self.empty_cursor:
                value["meta"]["next_cursor"] = ""
        else:
            raise AssertionError(f"unexpected partial request {path}")
        headers = {
            "x-markethub-dataset-version": self.publication["dataset_version"],
            "x-markethub-partial-completeness-revision": self.publication[
                "partial_completeness_revision"
            ],
            "x-markethub-generation-pin": self.publication["generation_pin"],
        }
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001, headers


class ProductPartitionedCoverageTransport:
    publication = PartialFuturesTransport.publication

    def __init__(self) -> None:
        self.coverage_codes: list[str] = []

    def request_json_with_headers(self, method, path, *, query=None, body=None):
        del body
        assert method == "GET"
        assert path == "/api/futures/quotes/1m/partial/coverage"
        assert query is not None
        code = str(query["codes"])
        self.coverage_codes.append(code)
        assert "," not in code, "production partial coverage times out for combined products"
        exchange = "SHFE" if code == "ag" else "DCE"
        value = {
            "items": [
                {
                    "product_code": code,
                    "exchange": exchange,
                    "start_time": "2025-01-02 09:01:00",
                    "end_time": "2025-01-02 09:03:00",
                    "status": "accepted",
                    "observed_count": 3,
                }
            ],
            "meta": {
                **self.publication,
                "catalog_identity": "qmf-catalog-v1-fixture",
                "missing_bar_semantics": "skip",
                "warmup": {"residual_semantics": "excluded_or_missing_rows_are_skipped"},
                "partial_contract_satisfied": True,
                "coverage_semantics": "observed_admitted_runs_only",
                "residual_semantics": "excluded_or_missing_rows_are_skipped",
                "next_cursor": None,
            },
        }
        headers = {
            "x-markethub-dataset-version": self.publication["dataset_version"],
            "x-markethub-partial-completeness-revision": self.publication[
                "partial_completeness_revision"
            ],
            "x-markethub-generation-pin": self.publication["generation_pin"],
        }
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001, headers


class MultiProductPartialTransport:
    publication = PartialFuturesTransport.publication

    def request_json_with_headers(self, method, path, *, query=None, body=None):
        del body
        assert method == "GET"
        assert query is not None
        code = str(query["codes"])
        assert code in {"ag", "i"}
        exchange = "SHFE" if code == "ag" else "DCE"
        meta = {
            **self.publication,
            "catalog_identity": "qmf-catalog-v1-fixture",
            "missing_bar_semantics": "skip",
            "warmup": {"residual_semantics": "excluded_or_missing_rows_are_skipped"},
            "partial_contract_satisfied": True,
            "next_cursor": None,
        }
        if path.endswith("/coverage"):
            meta.update(
                {
                    "coverage_semantics": "observed_admitted_runs_only",
                    "residual_semantics": "excluded_or_missing_rows_are_skipped",
                }
            )
            items = [
                {
                    "product_code": code,
                    "exchange": exchange,
                    "start_time": "2025-01-02 09:01:00",
                    "end_time": "2025-01-02 09:01:00",
                    "status": "accepted",
                    "observed_count": 1,
                }
            ]
        elif path.endswith("/partial"):
            meta.update(
                {
                    "qmi_id": "qmi-v1-fixture",
                    "source_boundary_manifest": {"count": 1, "sha256": "a" * 64},
                    "source_manifests": [{"source_key": "fixture", "lineage": "observed"}],
                    "lineage_limitations": "fixture partial data is not session complete",
                    "session_grid": "not_asserted_complete",
                    "coverage": {
                        "endpoint": "/api/futures/quotes/1m/partial/coverage",
                        "semantics": "observed_admitted_runs_only",
                        "residual_semantics": "excluded_or_missing_rows_are_skipped",
                    },
                }
            )
            items = [
                {
                    "product_code": code,
                    "exchange": exchange,
                    "series_type": "back_adjusted_continuous",
                    "bar_time": "2025-01-02 09:01:00",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "100",
                    "open_interest": None,
                    "adjustment_offset": "10",
                    "boundary_ids": ["qmb-v1-fixture"],
                    "source_keys": ["fixture"],
                }
            ]
        else:
            raise AssertionError(f"unexpected partial request {path}")
        value = {"items": items, "meta": meta}
        headers = {
            "x-markethub-dataset-version": self.publication["dataset_version"],
            "x-markethub-partial-completeness-revision": self.publication[
                "partial_completeness_revision"
            ],
            "x-markethub-generation-pin": self.publication["generation_pin"],
        }
        payload = json.dumps(value).encode()
        return deepcopy(value), len(payload), 0.001, headers


def snapshot(*, catalog_bound: bool = False) -> dict:
    return {
        "schema": "quant-research.market-snapshot-ref.v1",
        "snapshot_id": "sha256:" + "c" * 64,
        "mode": "reference",
        "trust_policy": "assumed_immutable",
        "source": {
            "adapter": "markethub",
            "adapter_version": "1.0.0",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
            "data_revision": (
                "future_bar_1m:fixture-futures-v1;future_contract_reference:fixture-contracts-v1"
                if catalog_bound
                else "future_bar_1m:fixture-futures-v1"
            ),
        },
        "query": {
            "instruments": ["agL0"],
            "start": "2025-01-02",
            "end": "2025-01-02",
            "frequency": "1m",
            "adjustment": "back_adjusted",
        },
        "calendar": "cn-futures-v1",
        "contract_mapping": "back_adjusted_continuous",
        "resolved_at": "2026-08-24T00:00:00Z",
    }


def formal_config(*, catalog_bound: bool = False) -> dict:
    config = {
        "execution": {
            "initial_cash_cny": "5000000",
            "slippage_ticks": "1.5",
            "trading_days": ["2025-01-02"],
            "contracts": {
                "agL0": {
                    "product_code": "ag",
                    "exchange": "SHFE",
                    "asset_class": "COMMODITY",
                    "currency": "CNY",
                    "price_precision": 0,
                    "tick_size": "1",
                    "multiplier": "15",
                    "lot_size": "1",
                    "margin_init": "0.13",
                    "margin_maint": "0.10",
                    "commission": {
                        "open": {"per_contract": "0.5", "rate": "0"},
                        "close": {"per_contract": "0.6", "rate": "0"},
                        "close_today": {"per_contract": "0.7", "rate": "0"},
                    },
                }
            },
        }
    }
    if catalog_bound:
        config["execution"].update(
            {
                "coverage": {
                    "agL0": {
                        "rows": 3,
                        "first_bar_time": "2025-01-02 09:01:00",
                        "last_bar_time": "2025-01-02 09:03:00",
                    }
                },
                "profile": {
                    "schema": "quant-runtime.cn-futures-execution-profile.v1",
                    "contract_catalog": {
                        "schema_version": "future_contract_catalog_v2",
                        "dataset_version": "fixture-contracts-v1",
                        "snapshot_id": "fixture-contract-snapshot",
                        "content_checksum": "d" * 64,
                    },
                    "commission_margin": {
                        "source_id": "fixture-costs-v1",
                        "source_sha256": "e" * 64,
                        "effective_at": "2025-01-02",
                        "decoder": "fixture-decoder-v1",
                    },
                    "historical_rate_policy": "frozen_profile_not_point_in_time",
                    "margin_maint_policy": "equal_to_source_single_margin",
                    "lot_size_policy": "one_contract",
                    "close_priority": "yesterday_first",
                },
            }
        )
    return config


def request(
    package_ref: dict,
    config: dict | None = None,
    *,
    catalog_bound: bool = False,
) -> dict:
    return {
        "schema": "quant-research.workspace-run-request.v2",
        "strategy_package": package_ref,
        "market_snapshot": snapshot(catalog_bound=catalog_bound),
        "parameters": {},
        "execution": {
            "topology": "formal_only",
            "formal": [
                {"id": "primary", "adapter": "nautilus", "config": config or formal_config()}
            ],
        },
    }


def partial_snapshot() -> dict:
    return {
        **snapshot(),
        "trust_policy": "verified_immutable",
        "source": {
            **snapshot()["source"],
            "endpoint_contract": "futures-1m-partial-v1",
            "data_revision": (
                "future_1m_partial_s000012_quotemux:qmp-v1-fixture;"
                "partial_completeness:qmc-v1-fixture;generation_pin:qmg-v1-fixture;"
                "qmi:qmi-v1-fixture;catalog:qmf-catalog-v1-fixture"
            ),
            "partial_publication": PartialFuturesTransport.publication,
        },
    }


def executor(workspace: Path, *, missing_offset: bool = False) -> RuntimeExecutor:
    return RuntimeExecutor(
        WorkspaceClient(workspace),
        WorkspaceWorker(workspace),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(
                transport=FuturesTransport(missing_offset=missing_offset)
            )
        ),
    )


def test_futures_capability_profile_and_native_execution(tmp_path: Path) -> None:
    capabilities = production_registry().profile("formal", "nautilus").capabilities
    assert {
        "data.bar.1m",
        "data.futures.adjustment_offset",
        "data.futures.contract_catalog",
        "decision.order",
        "decision.target_contracts",
        "market.cn.futures",
        "market.cn.futures.continuous",
        "position.long_short",
    } <= capabilities

    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(request(package["package_ref"]))
    completed = executor(workspace).execute(submitted["run_id"])

    assert completed["status"] == "completed"
    metrics = completed["result"]["formal"]["primary"]["metrics"]
    assert metrics["native_order_report_rows"] >= 1
    assert metrics["native_fill_report_rows"] >= 1
    assert metrics["native_position_report_rows"] >= 1
    assert metrics["native_account_report_rows"] >= 1
    assert metrics["futures_data_loading"] == "nautilus_native_streaming_v1"
    assert metrics["streaming_chunks"] == 1
    assert metrics["streamed_native_events"] == 6
    assert metrics["peak_streaming_batch_bars"] == 3
    assert metrics["futures_execution_profile_hash"] is None
    statistics_ref = next(
        item
        for item in completed["result"]["artifacts"]
        if item["name"].endswith("native_statistics.json")
    )
    assert statistics_ref["record_schema"] == "quant-runtime.nautilus-reporting-input.v1"
    statistics_payload = client.read_artifact(statistics_ref["uri"])
    statistics = json.loads(base64.b64decode(statistics_payload["content"]))
    assert statistics["schema"] == "quant-runtime.nautilus-reporting-input.v1"
    assert statistics["extraction"]["engine_version"] == "1.231.0"
    assert {"stats_pnls", "stats_returns", "stats_general"} <= statistics.keys()
    assert statistics["run_info"]["total_orders"] >= 1
    assert statistics["account_info"]
    decision_ref = next(
        item
        for item in completed["result"]["artifacts"]
        if item["name"].endswith("strategy_decisions.json")
    )
    payload = client.read_artifact(decision_ref["uri"])
    decisions = json.loads(base64.b64decode(payload["content"]))
    assert decisions["schema"] == "quant-runtime.nautilus-observed-decisions.v2"
    assert decisions["decisions"][0]["intent"] == "order"


def test_futures_snapshot_ignores_unrelated_global_version_drift() -> None:
    client = MarketHubClient(transport=FuturesTransport(drift_unrelated_global_version=True))

    dataset = client.fetch_futures_dataset(
        ("agL0",),
        date(2025, 1, 2),
        date(2025, 1, 2),
        series_type="back_adjusted_continuous",
    )

    assert dataset.data_version == "future_bar_1m"
    assert dataset.dataset_version == "fixture-futures-v1"
    assert isinstance(dataset.bars, CanonicalFuturesBars)
    expanded = CanonicalFuturesDataset(
        data_version=dataset.data_version,
        dataset_version=dataset.dataset_version,
        timezone=dataset.timezone,
        series_type=dataset.series_type,
        instruments=dataset.instruments,
        bars=tuple(dataset.bars),
        contract_catalog=dataset.contract_catalog,
    )
    assert dataset.input_hash == expanded.input_hash


def test_catalog_bound_futures_execution_validates_native_specs_and_coverage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "catalog-bound-workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(
        request(
            package["package_ref"],
            formal_config(catalog_bound=True),
            catalog_bound=True,
        )
    )
    completed = executor(workspace).execute(submitted["run_id"])

    assert completed["status"] == "completed"
    metrics = completed["result"]["formal"]["primary"]["metrics"]
    assert metrics["futures_execution_profile_hash"]
    assert metrics["futures_contract_catalog_dataset_version"] == "fixture-contracts-v1"
    assert metrics["futures_contract_catalog_snapshot_id"] == "fixture-contract-snapshot"

    mismatch = formal_config(catalog_bound=True)
    mismatch["execution"]["contracts"]["agL0"]["multiplier"] = "16"
    failed_workspace = tmp_path / "catalog-mismatch-workspace"
    failed_client = WorkspaceClient(failed_workspace)
    failed_package = failed_client.register_package(PACKAGE)
    failed_run = failed_client.submit_run(
        request(failed_package["package_ref"], mismatch, catalog_bound=True)
    )
    failed = executor(failed_workspace).execute(failed_run["run_id"])
    assert failed["status"] == "failed"
    assert "native contract spec mismatch" in failed["error"]["message"]


def test_futures_contract_catalog_fails_closed_on_missing_native_spec() -> None:
    client = MarketHubClient(transport=FuturesTransport(missing_catalog_tick=True))

    with pytest.raises(MarketHubContractError, match="native specs"):
        client.fetch_futures_dataset(
            ("agL0",),
            date(2025, 1, 2),
            date(2025, 1, 2),
            series_type="back_adjusted_continuous",
        )


def test_futures_fails_closed_on_missing_contract_specs_and_adjustment_offset(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "missing-spec-workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    broken = formal_config()
    del broken["execution"]["contracts"]["agL0"]["multiplier"]
    submitted = client.submit_run(request(package["package_ref"], broken))
    failed = executor(workspace).execute(submitted["run_id"])
    assert failed["status"] == "failed"
    assert "multiplier" in failed["error"]["message"]

    offset_workspace = tmp_path / "missing-offset-workspace"
    offset_client = WorkspaceClient(offset_workspace)
    offset_package = offset_client.register_package(PACKAGE)
    offset_submitted = offset_client.submit_run(request(offset_package["package_ref"]))
    offset_failed = executor(offset_workspace, missing_offset=True).execute(
        offset_submitted["run_id"]
    )
    assert offset_failed["status"] == "failed"
    assert "numeric value is null" in offset_failed["error"]["message"]


def test_partial_futures_snapshot_uses_only_public_partial_contract_and_preserves_lineage(
    tmp_path: Path,
) -> None:
    transport = PartialFuturesTransport()
    manifest = partial_snapshot()
    request_value = SnapshotRequest.from_manifest(manifest)
    adapter = MarketHubDataAdapter(client_factory=lambda _: MarketHubClient(transport=transport))
    verification = adapter.read(request_value, expected_revision=None)

    assert transport.paths == [
        "/api/futures/quotes/1m/partial/coverage",
        "/api/futures/quotes/1m/partial",
    ]
    assert verification.dataset.dataset_version == "qmp-v1-fixture"
    assert verification.dataset.partial_lineage is not None
    assert verification.dataset.partial_lineage["qmi_id"] == "qmi-v1-fixture"
    assert verification.dataset.partial_lineage["missing_bar_semantics"] == "skip"

    output = tmp_path / "partial-formal"
    result = run_futures_engine(
        verification.dataset,
        FuturesExecutionConfig.from_dict(formal_config()["execution"]),
        StrategyContext("test.partial", 1, "a" * 64, "b" * 64, {}),
        output,
        strategy_class=load_package_entrypoint(PACKAGE, "strategy.py:NativeFuturesFixtureStrategy"),
        decision_intents=frozenset({"order"}),
    )

    assert result.metrics["partial_snapshot"]["qmi_id"] == "qmi-v1-fixture"
    lineage = json.loads((output / "partial_snapshot_lineage.json").read_text())
    assert lineage["missing_bar_semantics"] == "skip"
    statistics = json.loads((output / "native_statistics.json").read_text())
    assert statistics["partial_snapshot_lineage"]["catalog_identity"] == "qmf-catalog-v1-fixture"


def test_partial_futures_fails_closed_on_repeated_cursor() -> None:
    with pytest.raises(MarketHubContractError, match="cursor is invalid or repeated"):
        MarketHubClient(
            transport=PartialFuturesTransport(repeated_cursor=True)
        ).fetch_partial_futures_dataset(
            ("agL0",),
            date(2025, 1, 2),
            date(2025, 1, 2),
            series_type="back_adjusted_continuous",
            publication=SnapshotRequest.from_manifest(partial_snapshot()).partial_publication,
        )


def test_partial_futures_paginates_coverage_and_preserves_stream_hash() -> None:
    transport = PartialFuturesTransport(paged=True)
    dataset = MarketHubClient(transport=transport).fetch_partial_futures_dataset(
        ("agL0",),
        date(2025, 1, 2),
        date(2025, 1, 2),
        series_type="back_adjusted_continuous",
        publication=SnapshotRequest.from_manifest(partial_snapshot()).partial_publication,
    )

    assert transport.paths[:4] == [
        "/api/futures/quotes/1m/partial/coverage",
        "/api/futures/quotes/1m/partial/coverage",
        "/api/futures/quotes/1m/partial",
        "/api/futures/quotes/1m/partial",
    ]
    expanded = CanonicalFuturesDataset(
        data_version=dataset.data_version,
        dataset_version=dataset.dataset_version,
        timezone=dataset.timezone,
        series_type=dataset.series_type,
        instruments=dataset.instruments,
        bars=tuple(dataset.bars),
        partial_lineage=dataset.partial_lineage,
    )
    assert dataset.input_hash == expanded.input_hash
    assert dataset.bar_counts == {"agL0": 3}


def test_partial_futures_partitions_multi_product_coverage_queries() -> None:
    transport = ProductPartitionedCoverageTransport()
    publication = SnapshotRequest.from_manifest(partial_snapshot()).partial_publication
    assert publication is not None
    revision = (
        f"{publication.dataset_id}:{publication.dataset_version};"
        f"partial_completeness:{publication.partial_completeness_revision};"
        f"generation_pin:{publication.generation_pin};"
        "qmi:qmi-v1-fixture;catalog:qmf-catalog-v1-fixture"
    )

    dataset = MarketHubClient(transport=transport).open_partial_futures_stream(
        ("agL0", "iL0"),
        date(2025, 1, 2),
        date(2025, 1, 2),
        series_type="back_adjusted_continuous",
        publication=publication,
        verification={"canonical_input_hash": "a" * 64},
        expected_revision=revision,
    )

    assert transport.coverage_codes == ["ag", "i"]
    assert tuple(item.instrument for item in dataset.instruments) == ("agL0", "iL0")


def test_partial_snapshot_open_preflights_without_replaying_bars(tmp_path: Path) -> None:
    transport = PartialFuturesTransport(paged=True)
    adapter = MarketHubDataAdapter(client_factory=lambda _: MarketHubClient(transport=transport))
    request_value = SnapshotRequest.from_manifest(partial_snapshot())
    resolved = adapter.resolve(request_value, AdapterStorage.create(tmp_path / "resolve"))
    reads_after_resolution = transport.bar_reads

    opened = adapter.open_snapshot(
        resolved.manifest,
        AdapterStorage.create(tmp_path / "open"),
    )

    assert opened.dataset is not None
    assert transport.bar_reads == reads_after_resolution
    assert transport.coverage_reads == 4
    assert tuple(opened.dataset.bars)
    assert transport.bar_reads == reads_after_resolution + 2


def test_partial_snapshot_verification_is_independent_of_request_order(tmp_path: Path) -> None:
    transport = MultiProductPartialTransport()
    adapter = MarketHubDataAdapter(client_factory=lambda _: MarketHubClient(transport=transport))
    manifest = partial_snapshot()
    manifest["query"]["instruments"] = ["iL0", "agL0"]
    request_value = SnapshotRequest.from_manifest(manifest)
    resolved = adapter.resolve(request_value, AdapterStorage.create(tmp_path / "resolve"))

    opened = adapter.open_snapshot(
        resolved.manifest,
        AdapterStorage.create(tmp_path / "open"),
    )

    assert opened.dataset is not None
    assert tuple(opened.dataset.bars)


@pytest.mark.parametrize("kwargs", [{"empty_cursor": True}, {"coverage_count_delta": 1}])
def test_partial_futures_fails_closed_on_bad_cursor_or_coverage(kwargs: dict) -> None:
    with pytest.raises(MarketHubContractError):
        MarketHubClient(transport=PartialFuturesTransport(**kwargs)).fetch_partial_futures_dataset(
            ("agL0",),
            date(2025, 1, 2),
            date(2025, 1, 2),
            series_type="back_adjusted_continuous",
            publication=SnapshotRequest.from_manifest(partial_snapshot()).partial_publication,
        )


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"coverage_catalog_drift": True, "paged": True}, "lineage drifted"),
        ({"coverage_semantics_drift": True, "paged": True}, "coverage semantics drifted"),
        ({"missing_bars_lineage": True}, "lineage is incomplete"),
    ],
)
def test_partial_futures_fails_closed_on_coverage_or_bars_lineage_drift(
    kwargs: dict, match: str
) -> None:
    with pytest.raises(MarketHubContractError, match=match):
        MarketHubClient(transport=PartialFuturesTransport(**kwargs)).fetch_partial_futures_dataset(
            ("agL0",),
            date(2025, 1, 2),
            date(2025, 1, 2),
            series_type="back_adjusted_continuous",
            publication=SnapshotRequest.from_manifest(partial_snapshot()).partial_publication,
        )


def test_partial_futures_stream_fails_closed_on_second_pass_lineage_drift() -> None:
    transport = PartialFuturesTransport(paged=True, lineage_drift_after_scan=True)
    dataset = MarketHubClient(transport=transport).fetch_partial_futures_dataset(
        ("agL0",),
        date(2025, 1, 2),
        date(2025, 1, 2),
        series_type="back_adjusted_continuous",
        publication=SnapshotRequest.from_manifest(partial_snapshot()).partial_publication,
    )

    with pytest.raises(MarketHubContractError, match="lineage drifted"):
        tuple(dataset.bars)


def test_streaming_batches_do_not_split_same_timestamp() -> None:
    template = CanonicalFuturesBar(
        bar_time=datetime(2025, 1, 2, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        instrument="agL0",
        signal_open=Decimal("100"),
        signal_high=Decimal("101"),
        signal_low=Decimal("99"),
        signal_close=Decimal("100"),
        volume=Decimal("1"),
        open_interest=None,
        adjustment_offset=Decimal("10"),
    )
    same_time = (template, template, template)
    assert [len(batch) for batch in _signal_batches(same_time, batch_size=2)] == [3]


def test_signal_context_maps_night_session_to_frozen_trading_day() -> None:
    instrument = CanonicalFuturesInstrument(
        instrument="agL0",
        product_code="ag",
        exchange="SHFE",
        series_type="back_adjusted_continuous",
    )
    bar = CanonicalFuturesBar(
        bar_time=datetime(2025, 1, 1, 21, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        instrument="agL0",
        signal_open=Decimal("100"),
        signal_high=Decimal("101"),
        signal_low=Decimal("99"),
        signal_close=Decimal("100"),
        volume=Decimal("1"),
        open_interest=None,
        adjustment_offset=Decimal("10"),
    )
    dataset = CanonicalFuturesDataset(
        data_version="global-v1",
        dataset_version="futures-v1",
        timezone="Asia/Shanghai",
        series_type="back_adjusted_continuous",
        instruments=(instrument,),
        bars=(bar,),
    )
    raw = formal_config()["execution"]
    config = FuturesExecutionConfig.from_dict(raw)
    context = FuturesStrategyContext(
        strategy=StrategyContext("test", 1, "a" * 64, "b" * 64, {}),
        dataset=dataset,
        instruments={},
        contract_specs=config.contracts,
        execution=config,
    )
    signal = context.signal_bar(int(bar.bar_time.timestamp() * 1_000_000_000), "agL0")

    assert signal.trading_day == date(2025, 1, 2)
    assert signal.open_interest is None
    assert signal.signal_close == Decimal("100")
    assert signal.economic_close == Decimal("110")


def test_back_adjusted_signal_space_can_cross_zero_when_economic_prices_are_positive() -> None:
    bar = CanonicalFuturesBar(
        bar_time=datetime(2015, 10, 12, 14, 51, tzinfo=ZoneInfo("Asia/Shanghai")),
        instrument="jL0",
        signal_open=Decimal("1"),
        signal_high=Decimal("1"),
        signal_low=Decimal("0"),
        signal_close=Decimal("0.5"),
        volume=Decimal("2386"),
        open_interest=None,
        adjustment_offset=Decimal("741.5"),
    )

    bar.validate()

    assert bar.economic_low == Decimal("741.5")
