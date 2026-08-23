import json
from decimal import Decimal
from hashlib import sha256
from inspect import signature
from pathlib import Path

import pytest

from markethub_nautilus import momentum, runner
from markethub_nautilus.engine import run_engine
from markethub_nautilus.markethub import FetchMetrics
from markethub_nautilus.momentum import build_momentum_reference
from markethub_nautilus.momentum_strategy import MomentumTopKStrategy

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


@pytest.mark.engine
@pytest.mark.golden
def test_momentum_strategy_computes_reference_and_executes_with_native_engine(
    monkeypatch, tmp_path: Path, momentum_s_dataset
) -> None:
    config = runner.RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    expected = build_momentum_reference(momentum_s_dataset, config.strategy)
    assert "reference" not in signature(MomentumTopKStrategy).parameters
    assert "dataset" not in signature(MomentumTopKStrategy).parameters

    def forbidden_oracle(*args, **kwargs):
        raise AssertionError("the formal engine path must not consume the offline oracle")

    monkeypatch.setattr(momentum, "build_momentum_reference", forbidden_oracle)
    outputs = [
        run_engine(momentum_s_dataset, config, tmp_path / f"momentum-{index}") for index in range(3)
    ]
    assert expected.decision_hash == (
        "5446b519590fc2e047ea3dae7d24c3edfca1cb923c705d341652d8c40e038439"
    )
    assert outputs[0].decisions == [item.as_dict() for item in expected.decisions]
    assert outputs[0].fills
    assert len({output.output_hash for output in outputs}) == 1
    for index in range(3):
        artifact = tmp_path / f"momentum-{index}" / "strategy_decisions.json"
        assert json.loads(artifact.read_text()) == expected.envelope()


@pytest.mark.engine
def test_momentum_runner_indexes_reference_contract(
    monkeypatch, tmp_path: Path, momentum_s_dataset
) -> None:
    class FixtureClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url
            self.metrics = FetchMetrics()

        def fetch_dataset(self, instruments, start_date, end_date):
            return momentum_s_dataset

    monkeypatch.setattr(runner, "MarketHubClient", FixtureClient)
    config_path = ROOT / "configs" / "cross-sectional-momentum-topk.s.json"
    manifest, _ = runner.run(config_path, tmp_path)
    assert manifest["config_hash"] == sha256(config_path.read_bytes()).hexdigest()
    assert manifest["strategy_spec_hash"] == (
        "f06669db3f35dd2096456df51fb69707dea3fd50d53d828bdaff8e7833bccd6d"
    )
    assert manifest["metrics"]["reference_decision_hash"] == (
        "5446b519590fc2e047ea3dae7d24c3edfca1cb923c705d341652d8c40e038439"
    )
    assert "strategy_decisions.json" in {item["relative_path"] for item in manifest["artifacts"]}
