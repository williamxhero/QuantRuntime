from __future__ import annotations

import base64
import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy_workspace import WorkspaceClient, WorkspaceWorker

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubDataAdapter
from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesDataset,
    CanonicalFuturesInstrument,
)
from quant_runtime.adapters.formal.nautilus import FuturesExecutionConfig, FuturesStrategyContext
from quant_runtime.adapters.formal.nautilus.runner import StrategyContext
from quant_runtime.executor import RuntimeExecutor
from quant_runtime.registry import production_registry

PACKAGE = Path(__file__).parent / "fixtures" / "futures-native"


class FuturesTransport:
    def __init__(self, *, missing_offset: bool = False) -> None:
        self.missing_offset = missing_offset

    def request_json(self, method, path, *, query=None, body=None):
        del body
        if method == "GET" and path == "/api/health":
            value = {
                "status": "ok",
                "data_version": "fixture-global-v1",
                "dataset_versions": {
                    "future_bar_1m": "fixture-futures-v1",
                    "stock_daily_1d": "fixture-daily-v1",
                },
            }
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
                "limit": 500000,
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


def snapshot() -> dict:
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
            "data_revision": "fixture-global-v1:fixture-futures-v1",
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


def formal_config() -> dict:
    return {
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


def request(package_ref: dict, config: dict | None = None) -> dict:
    return {
        "schema": "quant-research.workspace-run-request.v2",
        "strategy_package": package_ref,
        "market_snapshot": snapshot(),
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
    decision_ref = next(
        item
        for item in completed["result"]["artifacts"]
        if item["name"].endswith("strategy_decisions.json")
    )
    payload = client.read_artifact(decision_ref["uri"])
    decisions = json.loads(base64.b64decode(payload["content"]))
    assert decisions["schema"] == "quant-runtime.nautilus-observed-decisions.v2"
    assert decisions["decisions"][0]["intent"] == "order"


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
