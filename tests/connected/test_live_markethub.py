from pathlib import Path

import pytest

from markethub_nautilus.config import RunConfig
from markethub_nautilus.engine import run_engine
from markethub_nautilus.markethub import MarketHubClient, MarketHubError
from markethub_nautilus.momentum import build_momentum_reference

ROOT = Path(__file__).parents[2]


@pytest.mark.connected
def test_live_s_dataset_is_complete_or_reports_read_model_blocker() -> None:
    config = RunConfig.load(ROOT / "configs" / "s-validation.json")
    client = MarketHubClient(config.data.base_url)
    try:
        dataset = client.fetch_dataset(
            config.data.instruments,
            config.data.start_date,
            config.data.end_date,
        )
    except MarketHubError as exc:
        if "READ_MODEL_NOT_READY" in str(exc):
            pytest.xfail("MarketHub daily-window blocked: READ_MODEL_NOT_READY")
        raise
    assert len(dataset.instruments) == 2
    assert len(dataset.trading_days) == 18
    assert len(dataset.bars) == 36
    assert len(dataset.input_hash) == 64


@pytest.mark.connected
def test_live_momentum_reference_matches_cross_framework_contract() -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    dataset = MarketHubClient(config.data.base_url).fetch_dataset(
        config.data.instruments,
        config.data.start_date,
        config.data.end_date,
    )
    references = [build_momentum_reference(dataset, config.strategy) for _ in range(3)]
    assert len(references[0].decisions) == 15
    assert references[0].decisions[0].as_dict() == {
        "signal_date": "2025-01-07",
        "instrument": "SH.600000",
        "target_weight": "1",
    }
    assert references[0].decisions[-1].as_dict() == {
        "signal_date": "2025-01-27",
        "instrument": "SH.600000",
        "target_weight": "1",
    }
    assert {reference.decision_hash for reference in references} == {
        "be36c06594f471c74dd67784b918ce14a18e235706e441b5d210ce6bbcfaaff8"
    }


@pytest.mark.connected
@pytest.mark.engine
def test_live_momentum_native_execution_is_deterministic(tmp_path: Path) -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    dataset = MarketHubClient(config.data.base_url).fetch_dataset(
        config.data.instruments,
        config.data.start_date,
        config.data.end_date,
    )
    outputs = [run_engine(dataset, config, tmp_path / f"repeat-{index}") for index in range(3)]
    assert {output.decision_hash for output in outputs} == {
        "be36c06594f471c74dd67784b918ce14a18e235706e441b5d210ce6bbcfaaff8"
    }
    assert len({output.output_hash for output in outputs}) == 1
    assert all(len(output.fills) == 5 for output in outputs)
