import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from markethub_nautilus.canonical import canonical_json
from markethub_nautilus.config import RunConfig
from markethub_nautilus.momentum import build_momentum_reference

ROOT = Path(__file__).parents[2]


@pytest.mark.golden
def test_momentum_reference_matches_cross_framework_contract(momentum_s_dataset) -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    expected = json.loads(
        (ROOT / "tests" / "golden" / "cross-sectional-momentum-topk.s.json").read_text()
    )
    reference = build_momentum_reference(momentum_s_dataset, config.strategy)
    assert reference.envelope() == expected
    assert reference.decision_hash == sha256(canonical_json(expected)).hexdigest()
    assert (
        reference.decision_hash
        == "5446b519590fc2e047ea3dae7d24c3edfca1cb923c705d341652d8c40e038439"
    )


@pytest.mark.golden
def test_momentum_excludes_suspended_rows_and_breaks_ties_by_instrument(
    momentum_s_dataset,
) -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    flat_bars = tuple(
        replace(
            bar,
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            pre_close=Decimal("10"),
            is_suspended=(
                bar.trading_day.isoformat() == "2025-01-07" and bar.instrument == "SH.600000"
            ),
        )
        for bar in momentum_s_dataset.bars
    )
    dataset = replace(momentum_s_dataset, bars=flat_bars)
    reference = build_momentum_reference(dataset, config.strategy)
    by_day = {item.signal_date.isoformat(): item for item in reference.decisions}
    assert by_day["2025-01-07"].instrument == "SZ.000001"
    assert by_day["2025-01-08"].instrument == "SH.600000"


def test_momentum_top_k_weights_use_canonical_decimal_strings(
    momentum_s_dataset,
) -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    spec_payload = {
        **config.strategy.spec_payload,
        "parameters": {"lookback_days": 3, "top_k": 2},
    }
    strategy = replace(
        config.strategy,
        parameters=spec_payload["parameters"],
        spec_payload=spec_payload,
    )
    reference = build_momentum_reference(momentum_s_dataset, strategy)
    assert {item.target_weight for item in reference.decisions} == {"0.5"}
