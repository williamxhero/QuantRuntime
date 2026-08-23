from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import pandas as pd
import qlib

from quant_runtime.contracts.canonical_hash import read_json
from quant_runtime.contracts.strategy_spec import StrategySpec, resolve_strategy_path
from quant_runtime.markethub.client import MarketHubClient
from quant_runtime.markethub.daily_data import CanonicalDataset

from .candidate_builder import select_top_k
from .metrics import qlib_risk, rank_ic
from .qlib_loader import load_frame


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    path: Path
    source_bytes: bytes
    strategy: StrategySpec
    base_url: str
    timeout_seconds: float
    page_size: int
    instruments: tuple[str, ...]
    start_date: date
    end_date: date
    minimum_observations: int
    minimum_mean_ic: float

    @classmethod
    def load(cls, path: Path) -> DiscoveryConfig:
        source_bytes = path.read_bytes()
        raw = read_json(path)
        if raw.get("schema") != "quant-runtime.discovery-config.v1":
            raise ValueError("unsupported discovery config schema")
        market_hub = _object(raw, "market_hub")
        universe = _object(raw, "universe")
        gate = _object(raw, "quick_gate_policy")
        instruments = tuple(str(item) for item in universe.get("instruments", []))
        config = cls(
            path=path.resolve(),
            source_bytes=source_bytes,
            strategy=StrategySpec.load(resolve_strategy_path(path, raw.get("strategy_spec"))),
            base_url=str(market_hub.get("base_url", "")).rstrip("/"),
            timeout_seconds=float(market_hub.get("timeout_seconds", 60)),
            page_size=int(market_hub.get("page_size", 50_000)),
            instruments=instruments,
            start_date=date.fromisoformat(str(raw.get("start_date", ""))),
            end_date=date.fromisoformat(str(raw.get("end_date", ""))),
            minimum_observations=int(gate.get("minimum_observations", 20)),
            minimum_mean_ic=float(gate.get("minimum_mean_ic", 0)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.base_url or self.timeout_seconds <= 0 or not 1 <= self.page_size <= 100_000:
            raise ValueError("invalid MarketHub discovery settings")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        if self.instruments != tuple(sorted(set(self.instruments))):
            raise ValueError("instruments must be unique and canonical-order sorted")
        if not self.instruments or any(not _valid_instrument(item) for item in self.instruments):
            raise ValueError("discovery requires canonical SH/SZ/BJ instruments")
        if self.strategy.top_k > len(self.instruments):
            raise ValueError("top_k exceeds discovery universe")
        if self.minimum_observations < 1:
            raise ValueError("minimum_observations must be positive")

    @property
    def config_hash(self) -> str:
        return sha256(self.source_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    dataset: CanonicalDataset
    signals: pd.DataFrame
    rank_ic: pd.Series
    candidates: pd.DataFrame
    risk: pd.DataFrame
    status: str
    metrics: dict[str, Any]


ClientFactory = Callable[[DiscoveryConfig], MarketHubClient]


def run_discovery(
    config: DiscoveryConfig,
    *,
    client_factory: ClientFactory | None = None,
) -> DiscoveryResult:
    factory = client_factory or (
        lambda item: MarketHubClient(item.base_url, timeout_seconds=item.timeout_seconds)
    )
    client = factory(config)
    dataset = client.fetch_dataset(
        config.instruments,
        config.start_date,
        config.end_date,
        page_size=config.page_size,
    )
    frame = load_frame(dataset)
    signals = build_signals(frame, config.strategy.lookback_days)
    scored = signals.dropna(subset=["score"])
    valid = signals.dropna(subset=["score", "label"])
    if scored.empty or valid.empty:
        raise ValueError("not enough observations to evaluate the discovery signal")
    ic = rank_ic(signals)
    candidates = select_top_k(signals, config.strategy.top_k)
    risk = qlib_risk(candidates)
    mean_ic = float(ic.mean()) if not ic.empty else math.nan
    observations = int(len(valid))
    passed = observations >= config.minimum_observations and (
        math.isfinite(mean_ic) and mean_ic >= config.minimum_mean_ic
    )
    return DiscoveryResult(
        dataset=dataset,
        signals=signals,
        rank_ic=ic,
        candidates=candidates,
        risk=risk,
        status="passed" if passed else "rejected",
        metrics={
            "framework_version": qlib.__version__,
            "observation_count": observations,
            "signal_days": int(scored.index.get_level_values("datetime").nunique()),
            "candidate_rows": int(len(candidates)),
            "mean_rank_ic": mean_ic,
            "quick_gate_passed": passed,
            "fetch": client.metrics.as_dict(),
        },
    )


def build_signals(frame: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    tradable = frame.loc[~frame["is_suspended"]]
    working = tradable[["close"]].copy().sort_index()
    close = working["close"].groupby(level="instrument", sort=False)
    working["score"] = close.pct_change(periods=lookback_days, fill_method=None)
    working["label"] = close.shift(-1) / working["close"] - 1.0
    return working[["score", "label"]]


def _object(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _valid_instrument(value: str) -> bool:
    prefix, separator, code = value.partition(".")
    return separator == "." and prefix in {"SH", "SZ", "BJ"} and len(code) == 6 and code.isdigit()
