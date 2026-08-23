from __future__ import annotations

from datetime import date
from pathlib import Path

from markethub_qlib.artifacts import write_successful_run
from markethub_qlib.client import MarketHubClient
from markethub_qlib.config import RunConfig
from markethub_qlib.workflow import run_discovery

ROOT = Path(__file__).parents[1]


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
    import json

    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "markethub-qlib.run-manifest.v1"
    assert manifest["framework"] == "Qlib"
    assert manifest["framework_version"] == "0.9.7"
    assert len(manifest["canonical_input_hash"]) == 64
    assert all(
        set(item) == {"relative_path", "sha256", "content_bytes"} for item in manifest["artifacts"]
    )
    assert all(item["content_bytes"] > 0 for item in manifest["artifacts"])


def test_dataset_fixture_covers_requested_window(fixture_client: MarketHubClient) -> None:
    fixture_client.open()
    days = fixture_client.fetch_calendar(date(2025, 1, 1), date(2025, 1, 31))
    assert days[0] == date(2025, 1, 2)
    assert days[-1] == date(2025, 1, 10)
