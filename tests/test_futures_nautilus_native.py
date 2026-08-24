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
from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesBars,
    CanonicalFuturesDataset,
    CanonicalFuturesInstrument,
)
from quant_runtime.adapters.formal.nautilus import FuturesExecutionConfig, FuturesStrategyContext
from quant_runtime.adapters.formal.nautilus.runner import StrategyContext
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
