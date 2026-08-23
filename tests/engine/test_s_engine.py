import json
from decimal import Decimal
from pathlib import Path

import pytest

from markethub_nautilus import runner
from markethub_nautilus.engine import run_engine
from markethub_nautilus.markethub import FetchMetrics

ROOT = Path(__file__).parents[2]


@pytest.mark.engine
@pytest.mark.golden
def test_s_fixture_matches_frozen_business_result_and_is_deterministic(
    tmp_path: Path, s_config, s_dataset
) -> None:
    expected = json.loads((ROOT / "tests" / "golden" / "s_expectation.json").read_text())
    outputs = [run_engine(s_dataset, s_config, tmp_path / f"repeat-{index}") for index in range(3)]
    first = outputs[0]
    assert len(s_dataset.trading_days) == expected["trading_days"]
    assert len(s_dataset.bars) == expected["bars"]
    assert len(first.decisions) == expected["decisions"]
    assert len(first.fills) == expected["fills"]
    assert sum(Decimal(item["amount"]) for item in first.fees) == Decimal(
        expected["total_fees_cny"]
    )
    assert first.account_curve[-1]["total"] == expected["final_account_cny"]
    assert {item["reason"] for item in first.rejects} >= set(expected["rejection_reasons"])
    assert len({item.output_hash for item in outputs}) == 1
    artifact_names = {
        "native_account.csv",
        "native_fills.csv",
        "native_orders.csv",
        "native_positions.csv",
        "native_statistics.json",
        "normalized_output.json",
    }
    assert {path.name for path in (tmp_path / "repeat-0").iterdir()} == artifact_names


@pytest.mark.engine
def test_runner_writes_success_manifest_with_all_native_artifacts(
    monkeypatch, tmp_path: Path, s_dataset
) -> None:
    class FixtureClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url
            self.metrics = FetchMetrics()

        def fetch_dataset(self, instruments, start_date, end_date):
            assert instruments == ("SH.600000", "SZ.000001")
            return s_dataset

    monkeypatch.setattr(runner, "MarketHubClient", FixtureClient)
    manifest, path = runner.run(ROOT / "configs" / "s-validation.json", tmp_path)
    assert path == tmp_path / "run_manifest.json"
    assert manifest["status"] == "success"
    assert manifest["schema"] == "markethub-nautilus.run-manifest.v1"
    assert manifest["canonical_input_hash"] == s_dataset.input_hash
    assert {item["relative_path"] for item in manifest["artifacts"]} == {
        "native_account.csv",
        "native_fills.csv",
        "native_orders.csv",
        "native_positions.csv",
        "native_statistics.json",
        "normalized_output.json",
    }
