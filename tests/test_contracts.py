from __future__ import annotations

import pandas as pd

from markethub_qlib.contracts import build_strategy_decisions


def test_decisions_use_score_descending_then_instrument_ascending() -> None:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SZ.000001"),
            (pd.Timestamp("2025-01-02"), "SH.600000"),
            (pd.Timestamp("2025-01-02"), "BJ.430001"),
        ],
        names=["datetime", "instrument"],
    )
    candidates = pd.DataFrame(
        {
            "score": [1.0, 1.0, 2.0],
            "label": [0.1, 0.2, 0.3],
            "target_weight": [1 / 3, 1 / 3, 1 / 3],
        },
        index=index,
    )

    contract = build_strategy_decisions(candidates, strategy_spec_hash="spec-hash")

    assert [row["instrument"] for row in contract["decisions"]] == [
        "BJ.430001",
        "SH.600000",
        "SZ.000001",
    ]
    assert {row["target_weight"] for row in contract["decisions"]} == {
        "0.333333333333"
    }
    assert all("label" not in row and "score" not in row for row in contract["decisions"])
