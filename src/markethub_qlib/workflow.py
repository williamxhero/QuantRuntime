from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import qlib
from qlib.contrib.evaluate import risk_analysis
from qlib.contrib.evaluate_portfolio import get_rank_ic

from .client import LoadedDataset, MarketHubClient
from .config import RunConfig


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    dataset: LoadedDataset
    signals: pd.DataFrame
    ic: pd.Series
    candidates: pd.DataFrame
    risk: pd.DataFrame
    status: str
    metrics: dict[str, Any]


ClientFactory = Callable[[RunConfig], MarketHubClient]


def run_discovery(
    config: RunConfig,
    *,
    client_factory: ClientFactory | None = None,
) -> DiscoveryResult:
    factory = client_factory or _default_client_factory
    client = factory(config)
    dataset = client.load_qlib_frame(
        universe_kind=config.universe_kind,
        codes=config.codes,
        start_date=config.start_date,
        end_date=config.end_date,
        fields=config.fields,
        page_size=config.page_size,
    )
    signals = _build_signals(dataset.frame, config.lookback_days)
    scored = signals.dropna(subset=["score"])
    valid = signals.dropna(subset=["score", "label"])
    if scored.empty or valid.empty:
        raise ValueError("not enough observations to evaluate the discovery signal")

    # Upstream Qlib owns the cross-sectional rank-IC calculation primitive.
    ic = valid.groupby(level="datetime", sort=True).apply(_qlib_rank_ic)
    ic.name = "rank_ic"
    candidates = _select_top_k(scored, config.top_k)
    evaluated_candidates = candidates.dropna(subset=["label"])
    daily_returns = evaluated_candidates.groupby(level="datetime", sort=True)["label"].mean()

    # Upstream Qlib owns the standard daily risk analysis export.
    risk = risk_analysis(daily_returns, freq="day", mode="sum")
    mean_ic = float(ic.mean()) if not ic.empty else math.nan
    observation_count = int(len(valid))
    passed = observation_count >= config.minimum_observations and (
        math.isfinite(mean_ic) and mean_ic >= config.minimum_mean_ic
    )
    metrics = {
        "framework_version": qlib.__version__,
        "observation_count": observation_count,
        "signal_days": int(scored.index.get_level_values("datetime").nunique()),
        "candidate_rows": int(len(candidates)),
        "mean_rank_ic": mean_ic,
        "quick_gate_passed": passed,
        "fetch": dataset.metrics,
    }
    return DiscoveryResult(
        dataset=dataset,
        signals=signals,
        ic=ic,
        candidates=candidates,
        risk=risk,
        status="passed" if passed else "rejected",
        metrics=metrics,
    )


def _default_client_factory(config: RunConfig) -> MarketHubClient:
    return MarketHubClient(config.base_url, timeout_seconds=config.timeout_seconds)


def _build_signals(frame: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    tradable = frame.loc[~frame["is_suspended"]]
    working = tradable[["close"]].copy().sort_index()
    close_by_instrument = working["close"].groupby(level="instrument", sort=False)
    working["score"] = close_by_instrument.pct_change(
        periods=lookback_days,
        fill_method=None,
    )
    working["label"] = close_by_instrument.shift(-1) / working["close"] - 1.0
    return working[["score", "label"]]


def _qlib_rank_ic(group: pd.DataFrame) -> float:
    if len(group) < 2 or group["score"].nunique() < 2 or group["label"].nunique() < 2:
        return math.nan
    return float(get_rank_ic(group["score"], group["label"]))


def _select_top_k(signals: pd.DataFrame, top_k: int) -> pd.DataFrame:
    selected = (
        signals.reset_index()
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
