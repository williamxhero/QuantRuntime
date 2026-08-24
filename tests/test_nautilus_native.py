from __future__ import annotations

import base64
import json
from pathlib import Path

from conftest import PACKAGE, FixtureTransport
from strategy_workspace import WorkspaceClient, WorkspaceWorker
from test_executor_topologies import request

from quant_runtime.adapters.data.markethub import MarketHubClient, MarketHubDataAdapter
from quant_runtime.executor import RuntimeExecutor


def test_nautilus_preserves_native_evidence_and_observed_bar_decisions(
    tmp_path: Path,
    market_fixture: dict,
) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(request(package["package_ref"], "formal_only"))
    completed = RuntimeExecutor(
        client,
        WorkspaceWorker(workspace),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(market_fixture))
        ),
    ).execute(submitted["run_id"])
    assert completed["status"] == "completed"
    metrics = completed["result"]["formal"]["primary"]["metrics"]
    assert metrics["formal_decision_hash"]
    evidence = completed["result"]["artifacts"]
    index_ref = next(item for item in evidence if item["name"].endswith("evidence_index.json"))
    index_payload = client.read_artifact(index_ref["uri"])
    index = json.loads(base64.b64decode(index_payload["content"]))
    names = {item["path"] for item in index["files"]}
    assert "native_orders.csv" in names
    assert "native_statistics.json" in names
    assert "strategy_decisions.json" in names
    assert metrics["native_order_report_rows"] == 0
    assert metrics["native_fill_report_rows"] == 0
    assert metrics["native_position_report_rows"] == 0
    statistics_ref = next(
        item for item in evidence if item["name"].endswith("native_statistics.json")
    )
    assert statistics_ref["record_schema"] == "quant-runtime.nautilus-reporting-input.v1"
    statistics_payload = client.read_artifact(statistics_ref["uri"])
    statistics = json.loads(base64.b64decode(statistics_payload["content"]))
    assert statistics["schema"] == "quant-runtime.nautilus-reporting-input.v1"
    assert statistics["extraction"]["engine_version"] == "1.231.0"
    assert statistics["portfolio_returns"] == []
    assert statistics["availability"]["portfolio_returns"]["status"] == "unavailable"
    assert {"stats_pnls", "stats_returns", "stats_general"} <= statistics.keys()
    normalized_ref = next(
        item for item in evidence if item["name"].endswith("normalized_output.json")
    )
    normalized_payload = client.read_artifact(normalized_ref["uri"])
    normalized = json.loads(base64.b64decode(normalized_payload["content"]))
    assert normalized["native_statistics"] == statistics
    decision_ref = next(
        item for item in evidence if item["name"].endswith("strategy_decisions.json")
    )
    decision_payload = client.read_artifact(decision_ref["uri"])
    decisions = json.loads(base64.b64decode(decision_payload["content"]))
    assert decisions["observed_by"] == "NautilusTrader"
    assert decisions["decisions"]
