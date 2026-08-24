from __future__ import annotations

import pandas as pd


def build_signals(frame: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Build the observed-bar momentum feature used by package-owned Qlib recipes."""
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    tradable = frame.loc[~frame["is_suspended"]]
    working = tradable[["close"]].copy().sort_index()
    close = working["close"].groupby(level="instrument", sort=False)
    working["score"] = close.pct_change(periods=lookback_days, fill_method=None)
    working["label"] = close.shift(-1) / working["close"] - 1.0
    return working[["score", "label"]]
