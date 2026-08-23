from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256
from typing import Any

from .canonical import CanonicalDataset, canonical_json, normalize_decimal
from .config import StrategySpec

WEIGHT_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class RankedDecision:
    signal_date: date
    instrument: str
    target_weight: str
    score: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "signal_date": self.signal_date.isoformat(),
            "instrument": self.instrument,
            "target_weight": self.target_weight,
        }


@dataclass(frozen=True, slots=True)
class MomentumReference:
    strategy_spec_hash: str
    decisions: tuple[RankedDecision, ...]

    def envelope(self) -> dict[str, Any]:
        return {
            "schema": "canonical-strategy-decisions.v1",
            "strategy_spec_hash": self.strategy_spec_hash,
            "decisions": [item.as_dict() for item in self.decisions],
        }

    @property
    def decision_hash(self) -> str:
        return sha256(canonical_json(self.envelope())).hexdigest()


def build_momentum_reference(
    dataset: CanonicalDataset,
    spec: StrategySpec,
) -> MomentumReference:
    if spec.kind != "cross_sectional_momentum_topk":
        raise ValueError("momentum reference requires the formal momentum strategy")
    lookback_days = spec.parameters["lookback_days"]
    top_k = spec.parameters["top_k"]
    by_instrument: dict[str, list[object]] = {
        instrument.instrument: [] for instrument in dataset.instruments
    }
    for bar in dataset.bars:
        if not bar.is_suspended:
            by_instrument[bar.instrument].append(bar)
    candidates_by_day: dict[date, list[tuple[Decimal, str]]] = {}
    for instrument, bars in by_instrument.items():
        bars.sort(key=lambda item: item.trading_day)
        for index in range(lookback_days, len(bars)):
            current = bars[index]
            previous = bars[index - lookback_days]
            score = current.close / previous.close - Decimal(1)
            candidates_by_day.setdefault(current.trading_day, []).append((score, instrument))
    decisions: list[RankedDecision] = []
    for signal_date in sorted(candidates_by_day):
        ranked = sorted(candidates_by_day[signal_date], key=lambda item: (-item[0], item[1]))
        selected = ranked[:top_k]
        weight = canonical_weight(len(selected))
        decisions.extend(
            RankedDecision(
                signal_date=signal_date,
                instrument=instrument,
                target_weight=weight,
                score=score,
            )
            for score, instrument in selected
        )
    return MomentumReference(strategy_spec_hash=spec.spec_hash, decisions=tuple(decisions))


def canonical_weight(count: int) -> str:
    if count < 1:
        raise ValueError("cannot assign weights to an empty selection")
    value = (Decimal(1) / Decimal(count)).quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN)
    return normalize_decimal(value)
