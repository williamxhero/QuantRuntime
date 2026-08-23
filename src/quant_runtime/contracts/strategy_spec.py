from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical_hash import read_json, sha256_value

STRATEGY_SCHEMA = "quant-runtime.strategy-spec.v1"


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: str
    revision: str
    lookback_days: int
    top_k: int
    rebalance_frequency: str
    signal_timing: str
    execution_timing: str
    price_adjustment: str

    @classmethod
    def from_parameters(
        cls,
        strategy_id: str,
        revision: int,
        parameters: dict[str, Any],
    ) -> StrategySpec:
        spec = cls(
            strategy_id=(
                "cross-sectional-momentum-topk"
                if strategy_id == "equity.cross-sectional-momentum-topk"
                else strategy_id
            ),
            revision=str(revision),
            lookback_days=int(parameters.get("lookback_days", 0)),
            top_k=int(parameters.get("top_k", 0)),
            rebalance_frequency=str(parameters.get("rebalance_frequency", "")),
            signal_timing=str(parameters.get("signal_timing", "")),
            execution_timing=str(parameters.get("execution_timing", "")),
            price_adjustment=str(parameters.get("price_adjustment", "")),
        )
        spec.validate()
        return spec

    @classmethod
    def load(cls, path: Path) -> StrategySpec:
        raw = read_json(path)
        if raw.get("schema") != STRATEGY_SCHEMA:
            raise ValueError(f"unsupported strategy schema {raw.get('schema')!r}")
        parameters = raw.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("strategy parameters must be an object")
        spec = cls(
            strategy_id=str(raw.get("strategy_id", "")),
            revision=str(raw.get("revision", "")),
            lookback_days=int(parameters.get("lookback_days", 0)),
            top_k=int(parameters.get("top_k", 0)),
            rebalance_frequency=str(parameters.get("rebalance_frequency", "")),
            signal_timing=str(raw.get("signal_timing", "")),
            execution_timing=str(raw.get("execution_timing", "")),
            price_adjustment=str(raw.get("price_adjustment", "")),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.strategy_id != "cross-sectional-momentum-topk" or self.revision != "1":
            raise ValueError("unsupported strategy id or revision")
        if self.lookback_days < 1 or self.top_k < 1:
            raise ValueError("lookback_days and top_k must be positive")
        if self.rebalance_frequency != "daily":
            raise ValueError("revision 1 supports daily rebalancing only")
        if self.signal_timing != "close" or self.execution_timing != "next_open":
            raise ValueError("revision 1 requires close signal and next-open execution")
        if self.price_adjustment != "none":
            raise ValueError("revision 1 supports unadjusted MarketHub prices only")

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "schema": STRATEGY_SCHEMA,
            "strategy_id": self.strategy_id,
            "revision": self.revision,
            "parameters": {
                "lookback_days": self.lookback_days,
                "top_k": self.top_k,
                "rebalance_frequency": self.rebalance_frequency,
            },
            "signal_timing": self.signal_timing,
            "execution_timing": self.execution_timing,
            "price_adjustment": self.price_adjustment,
        }

    @property
    def spec_hash(self) -> str:
        return sha256_value(self.payload)


def resolve_strategy_path(config_path: Path, configured: Any) -> Path:
    if not isinstance(configured, str) or not configured:
        raise ValueError("strategy_spec must be a non-empty path")
    return (config_path.parent / configured).resolve()
