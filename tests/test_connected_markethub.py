from pathlib import Path

import pytest

from markethub_qlib.config import RunConfig
from markethub_qlib.workflow import run_discovery


@pytest.mark.connected
def test_connected_two_stock_january_2025_smoke() -> None:
    config = RunConfig.load(Path(__file__).parents[1] / "configs" / "s-smoke.json")
    result = run_discovery(config)
    assert len(result.dataset.instruments) == 2
    assert len(result.dataset.trading_days) == 18
    assert len(result.dataset.frame) == 36
    assert len(result.dataset.canonical_input_hash) == 64
