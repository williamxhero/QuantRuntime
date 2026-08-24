from __future__ import annotations

import pandas as pd

from quant_runtime.adapters.data.markethub.model import CanonicalDataset


def load_frame(dataset: CanonicalDataset) -> pd.DataFrame:
    """Convert the shared canonical dataset to Qlib's in-memory indexed frame."""
    records = [
        {
            "datetime": pd.Timestamp(item.trading_day),
            "instrument": item.instrument,
            "open": float(item.open),
            "high": float(item.high),
            "low": float(item.low),
            "close": float(item.close),
            "volume": float(item.volume),
            "amount": float(item.amount),
            "pre_close": float(item.pre_close),
            "is_suspended": item.is_suspended,
            "is_st": item.is_st,
        }
        for item in dataset.bars
    ]
    if not records:
        raise ValueError("canonical dataset has no bars")
    frame = pd.DataFrame.from_records(records).set_index(["datetime", "instrument"])
    frame.index = frame.index.set_names(["datetime", "instrument"])
    return frame.sort_index()
