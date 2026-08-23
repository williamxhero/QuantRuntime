from pathlib import Path

import pytest

from markethub_nautilus.config import RunConfig
from markethub_nautilus.markethub import MarketHubClient, MarketHubError

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
