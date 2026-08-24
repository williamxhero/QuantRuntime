from __future__ import annotations

import pandas as pd


def select_top_k(signals: pd.DataFrame, top_k: int) -> pd.DataFrame:
    selected = (
        signals.dropna(subset=["score"])
        .reset_index()
        .sort_values(
            ["datetime", "score", "instrument"],
            ascending=[True, False, True],
            kind="stable",
        )
        .groupby("datetime", sort=True)
        .head(top_k)
        .copy()
    )
    counts = selected.groupby("datetime")["instrument"].transform("count")
    selected["target_weight"] = 1.0 / counts
    return selected.set_index(["datetime", "instrument"]).sort_index()
