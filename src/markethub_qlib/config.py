from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {"close", "pre_close"}
ALLOWED_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pre_close",
    "is_suspended",
    "is_st",
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    raw: dict[str, Any]
    strategy_id: str
    spec_revision: str
    base_url: str
    timeout_seconds: float
    page_size: int
    universe_kind: str
    codes: tuple[str, ...]
    start_date: date
    end_date: date
    fields: tuple[str, ...]
    lookback_days: int
    top_k: int
    minimum_observations: int
    minimum_mean_ic: float

    @classmethod
    def load(cls, path: Path) -> RunConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load run config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("run config must be a JSON object")
        if raw.get("schema") != "markethub-qlib.run-config.v1":
            raise ValueError("unsupported run config schema")
        universe = _object(raw, "universe")
        market_hub = _object(raw, "market_hub")
        parameters = _object(raw, "parameters")
        gate = _object(raw, "quick_gate_policy")
        kind = str(universe.get("kind", ""))
        codes = tuple(sorted({str(code) for code in universe.get("codes", [])}))
        fields = tuple(str(field) for field in raw.get("fields", []))
        config = cls(
            raw=raw,
            strategy_id=_required_string(raw, "strategy_id"),
            spec_revision=_required_string(raw, "spec_revision"),
            base_url=str(market_hub.get("base_url", "http://yosef-server:8803")).rstrip("/"),
            timeout_seconds=float(market_hub.get("timeout_seconds", 60.0)),
            page_size=int(market_hub.get("page_size", 50_000)),
            universe_kind=kind,
            codes=codes,
            start_date=date.fromisoformat(_required_string(raw, "start_date")),
            end_date=date.fromisoformat(_required_string(raw, "end_date")),
            fields=fields,
            lookback_days=int(parameters.get("lookback_days", 3)),
            top_k=int(parameters.get("top_k", 10)),
            minimum_observations=int(gate.get("minimum_observations", 20)),
            minimum_mean_ic=float(gate.get("minimum_mean_ic", 0.0)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.strategy_id != "cross-sectional-momentum-topk":
            raise ValueError("unsupported strategy_id")
        if self.spec_revision != "1":
            raise ValueError("unsupported spec_revision")
        if self.universe_kind not in {"codes", "all_a"}:
            raise ValueError("universe.kind must be codes or all_a")
        if self.universe_kind == "codes" and not self.codes:
            raise ValueError("codes universe cannot be empty")
        if any(len(code) != 6 or not code.isdigit() for code in self.codes):
            raise ValueError("A-share codes must be six digits")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        if not REQUIRED_FIELDS.issubset(self.fields):
            raise ValueError(f"fields must include {sorted(REQUIRED_FIELDS)}")
        unknown_fields = set(self.fields) - ALLOWED_FIELDS
        if unknown_fields:
            raise ValueError(f"unsupported fields: {sorted(unknown_fields)}")
        if not 1 <= self.page_size <= 100_000:
            raise ValueError("page_size must be between 1 and 100000")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.lookback_days < 1 or self.top_k < 1:
            raise ValueError("lookback_days and top_k must be positive")
        if self.minimum_observations < 1:
            raise ValueError("minimum_observations must be positive")

    @property
    def strategy_spec(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "spec_revision": self.spec_revision,
            "parameters": {
                "lookback_days": self.lookback_days,
                "top_k": self.top_k,
            },
        }


def _object(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _required_string(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value
