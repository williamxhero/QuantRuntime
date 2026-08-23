from pathlib import Path

import pytest

from markethub_qlib.canonical import sha256_value
from markethub_qlib.config import RunConfig
from markethub_qlib.contracts import build_strategy_decisions, decision_hash
from markethub_qlib.workflow import run_discovery


@pytest.mark.connected
def test_connected_two_stock_january_2025_smoke() -> None:
    config = RunConfig.load(Path(__file__).parents[1] / "configs" / "s-smoke.json")
    first = run_discovery(config)
    second = run_discovery(config)
    strategy_spec_hash = sha256_value(config.strategy_spec)
    first_decisions = build_strategy_decisions(
        first.candidates, strategy_spec_hash=strategy_spec_hash
    )
    second_decisions = build_strategy_decisions(
        second.candidates, strategy_spec_hash=strategy_spec_hash
    )

    assert len(first.dataset.instruments) == 2
    assert len(first.dataset.trading_days) == 18
    assert len(first.dataset.frame) == 36
    assert len(first.dataset.canonical_input_hash) == 64
    assert len(first_decisions["decisions"]) == 15
    assert decision_hash(first_decisions) == decision_hash(second_decisions)
