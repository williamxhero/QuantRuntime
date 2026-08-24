from __future__ import annotations

from typing import Any

import pandas as pd


def discover(frame: pd.DataFrame, parameters: dict[str, Any]) -> dict[str, Any]:
    del parameters
    signals = frame[["close"]].rename(columns={"close": "score"})
    signals["label"] = 0.0
    candidates = signals.iloc[:1].copy()
    rank_ic = pd.Series([0.0], name="rank_ic")
    risk = pd.DataFrame({"risk": [0.0]})
    return {"signals": signals, "rank_ic": rank_ic, "candidates": candidates, "risk": risk}
