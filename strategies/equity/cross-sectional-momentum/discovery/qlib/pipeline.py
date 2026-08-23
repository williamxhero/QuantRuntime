from __future__ import annotations

from typing import Any

import pandas as pd

from quant_runtime.discovery.qlib.candidate_builder import select_top_k
from quant_runtime.discovery.qlib.metrics import qlib_risk, rank_ic
from quant_runtime.discovery.qlib.workflow import build_signals


def discover(frame: pd.DataFrame, parameters: dict[str, Any]) -> dict[str, Any]:
    """Run the package-owned Qlib discovery recipe over a canonical in-memory frame."""
    signals = build_signals(frame, int(parameters["lookback_days"]))
    candidates = select_top_k(signals, int(parameters["top_k"]))
    return {
        "signals": signals,
        "rank_ic": rank_ic(signals),
        "candidates": candidates,
        "risk": qlib_risk(candidates),
    }
