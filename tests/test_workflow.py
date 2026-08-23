from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from markethub_qlib.artifacts import write_successful_run
from markethub_qlib.client import MarketHubClient
from markethub_qlib.config import RunConfig
from markethub_qlib.workflow import run_discovery

ROOT = Path(__file__).parents[1]
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "s_strategy_golden.json"


def test_workflow_calls_qlib_and_writes_manifest(
    fixture_client: MarketHubClient, tmp_path: Path
) -> None:
    config = RunConfig.load(ROOT / "configs" / "s-smoke.json")
    result = run_discovery(config, client_factory=lambda _: fixture_client)
    assert result.metrics["framework_version"] == "0.9.7"
    assert result.metrics["observation_count"] >= 5
    assert not result.ic.dropna().empty
    assert list(result.risk.columns) == ["risk"]

    written = write_successful_run(config, result, tmp_path)
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "markethub-qlib.run-manifest.v1"
    assert manifest["framework"] == "Qlib"
    assert manifest["framework_version"] == "0.9.7"
    assert len(manifest["canonical_input_hash"]) == 64
    assert all(
        set(item) == {"relative_path", "sha256", "content_bytes"} for item in manifest["artifacts"]
    )
    assert all(item["content_bytes"] > 0 for item in manifest["artifacts"])


def test_strategy_decisions_match_offline_golden(
    fixture_client: MarketHubClient, tmp_path: Path
) -> None:
    config = RunConfig.load(ROOT / "configs" / "s-smoke.json")
    result = run_discovery(config, client_factory=lambda _: fixture_client)
    written = write_successful_run(config, result, tmp_path)
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    strategy_spec = json.loads((tmp_path / "strategy_spec.json").read_text(encoding="utf-8"))
    decisions = json.loads((tmp_path / "strategy_decisions.json").read_text(encoding="utf-8"))

    assert strategy_spec == golden["strategy_spec"]
    assert manifest["strategy_spec_hash"] == golden["strategy_spec_hash"]
    assert decisions == golden["strategy_decisions"]
    assert manifest["metrics"]["reference_decision_hash"] == golden["reference_decision_hash"]
    artifact_names = {item["relative_path"] for item in manifest["artifacts"]}
    assert {"strategy_spec.json", "strategy_decisions.json"} <= artifact_names


def test_dataset_fixture_covers_requested_window(fixture_client: MarketHubClient) -> None:
    fixture_client.open()
    days = fixture_client.fetch_calendar(date(2025, 1, 1), date(2025, 1, 31))
    assert days[0] == date(2025, 1, 2)
    assert days[-1] == date(2025, 1, 10)
