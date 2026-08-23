from __future__ import annotations

import pandas as pd

from quant_runtime.markethub.daily_data import CanonicalDataset


def load_frame(dataset: CanonicalDataset) -> pd.DataFrame:
    """Convert the shared canonical dataset to Qlib's in-memory indexed frame."""
    return dataset.to_qlib_frame()
