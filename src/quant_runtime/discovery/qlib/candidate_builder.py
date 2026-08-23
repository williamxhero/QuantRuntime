from __future__ import annotations

from decimal import Decimal

import pandas as pd

from quant_runtime.semantics.decision_record import DecisionRecord, canonical_weight


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


def candidate_decisions(candidates: pd.DataFrame) -> list[DecisionRecord]:
    rows = candidates.reset_index().sort_values(
        ["datetime", "score", "instrument"],
        ascending=[True, False, True],
        kind="stable",
    )
    counts = rows.groupby("datetime")["instrument"].transform("count")
    return [
        DecisionRecord(
            signal_date=pd.Timestamp(row.datetime).date(),
            instrument=str(row.instrument),
            target_weight=canonical_weight(int(count)),
            score=Decimal(str(row.score)),
        )
        for row, count in zip(rows.itertuples(index=False), counts, strict=True)
    ]
