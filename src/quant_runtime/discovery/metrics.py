from __future__ import annotations

import math

import pandas as pd
from qlib.contrib.evaluate import risk_analysis
from qlib.contrib.evaluate_portfolio import get_rank_ic


def rank_ic(signals: pd.DataFrame) -> pd.Series:
    valid = signals.dropna(subset=["score", "label"])
    result = valid.groupby(level="datetime", sort=True).apply(_rank_ic_day)
    result.name = "rank_ic"
    return result


def qlib_risk(candidates: pd.DataFrame) -> pd.DataFrame:
    returns = (
        candidates.dropna(subset=["label"]).groupby(level="datetime", sort=True)["label"].mean()
    )
    return risk_analysis(returns, freq="day", mode="sum")


def _rank_ic_day(group: pd.DataFrame) -> float:
    if len(group) < 2 or group["score"].nunique() < 2 or group["label"].nunique() < 2:
        return math.nan
    return float(get_rank_ic(group["score"], group["label"]))
