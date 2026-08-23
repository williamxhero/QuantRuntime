from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_json, normalize_decimal, sha256_json


@dataclass(frozen=True, slots=True)
class DataConfig:
    base_url: str
    start_date: date
    end_date: date
    instruments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeeSpec:
    commission_rate: Decimal
    minimum_commission_cny: Decimal
    sell_stamp_duty_rate: Decimal
    currency_precision: int
    rounding_mode: str
    rounding_scope: str


@dataclass(frozen=True, slots=True)
class Decision:
    trading_day: date
    instrument: str
    signal: str
    target_quantity: int
    order_intent: str
    expected_rule: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_rule": self.expected_rule,
            "instrument": self.instrument,
            "order_intent": self.order_intent,
            "signal": self.signal,
            "target_quantity": self.target_quantity,
            "trading_day": self.trading_day.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RuleState:
    limit_up: bool = False
    limit_down: bool = False
    suspended: bool = False
    has_bar: bool = True
    before_listing: bool = False
    after_delisting: bool = False


@dataclass(frozen=True, slots=True)
class StrategySpec:
    name: str
    initial_cash_cny: Decimal
    lot_size: int
    tick_size: Decimal
    fees: FeeSpec
    decisions: tuple[Decision, ...]
    rule_overrides: dict[tuple[date, str], RuleState]
    source_payload: dict[str, Any]

    def validate(self, data: DataConfig) -> None:
        if self.lot_size != 100 or self.tick_size != Decimal("0.01"):
            raise ValueError("A-share runner requires 100-share lots and a 0.01 tick")
        if (
            self.fees.currency_precision != 2
            or self.fees.rounding_mode != "half_away_from_zero"
            or self.fees.rounding_scope != "per_fill"
        ):
            raise ValueError("fees require per-fill CNY cent half-away-from-zero rounding")
        if self.decisions != tuple(
            sorted(self.decisions, key=lambda item: (item.trading_day, item.instrument))
        ):
            raise ValueError("strategy decisions must be in canonical order")
        known = set(data.instruments)
        if any(item.instrument not in known for item in self.decisions):
            raise ValueError("decision references an unknown instrument")
        if any(item.target_quantity % self.lot_size for item in self.decisions):
            raise ValueError("decision target violates lot size")
        intents = {"buy_market_next_open", "sell_market_next_open"}
        if any(item.order_intent not in intents for item in self.decisions):
            raise ValueError("only market next-open intents are supported")

    @property
    def decision_hash(self) -> str:
        return sha256_json([item.as_dict() for item in self.decisions])

    @property
    def spec_hash(self) -> str:
        return sha256_json(_normalize(self.source_payload))


@dataclass(frozen=True, slots=True)
class RunConfig:
    data: DataConfig
    strategy: StrategySpec
    source_payload: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> RunConfig:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        if payload.get("schema") != "markethub-nautilus.run-config.v1":
            raise ValueError(f"unsupported config schema {payload.get('schema')!r}")
        data_payload = payload["data"]
        instruments = tuple(data_payload["instruments"])
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("data instruments must be unique and in canonical order")
        for instrument in instruments:
            prefix, separator, code = instrument.partition(".")
            if separator != "." or prefix not in {"SH", "SZ", "BJ"} or not code.isdigit():
                raise ValueError(f"invalid canonical A-share instrument {instrument!r}")
        data = DataConfig(
            base_url=str(data_payload["base_url"]).rstrip("/"),
            start_date=date.fromisoformat(data_payload["start"]),
            end_date=date.fromisoformat(data_payload["end"]),
            instruments=instruments,
        )
        strategy_payload = payload["strategy"]
        fee_payload = strategy_payload["fees"]
        decisions = tuple(
            Decision(
                trading_day=date.fromisoformat(row["trading_day"]),
                instrument=row["instrument"],
                signal=row["signal"],
                target_quantity=int(row["target_quantity"]),
                order_intent=row["order_intent"],
                expected_rule=row["expected_rule"],
            )
            for row in strategy_payload["actions"]
        )
        if strategy_payload.get("rule_overrides") and not strategy_payload.get(
            "allow_rule_overrides", False
        ):
            raise ValueError("rule_overrides require explicit allow_rule_overrides=true")
        overrides = {}
        for key, value in strategy_payload.get("rule_overrides", {}).items():
            day_text, instrument = key.split("|", maxsplit=1)
            overrides[(date.fromisoformat(day_text), instrument)] = RuleState(**value)
        strategy = StrategySpec(
            name=str(strategy_payload["name"]),
            initial_cash_cny=Decimal(strategy_payload["initial_cash_cny"]),
            lot_size=int(strategy_payload["execution"]["lot_size"]),
            tick_size=Decimal(strategy_payload["execution"]["tick_size"]),
            fees=FeeSpec(
                commission_rate=Decimal(fee_payload["commission_rate"]),
                minimum_commission_cny=Decimal(fee_payload["minimum_commission_cny"]),
                sell_stamp_duty_rate=Decimal(fee_payload["sell_stamp_duty_rate"]),
                currency_precision=int(fee_payload["currency_precision"]),
                rounding_mode=str(fee_payload["rounding_mode"]),
                rounding_scope=str(fee_payload["rounding_scope"]),
            ),
            decisions=decisions,
            rule_overrides=overrides,
            source_payload=strategy_payload,
        )
        if data.start_date > data.end_date:
            raise ValueError("data start date is after end date")
        strategy.validate(data)
        return cls(data=data, strategy=strategy, source_payload=payload)

    @property
    def config_hash(self) -> str:
        return sha256_json(_normalize(self.source_payload))


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def canonical_config_bytes(config: RunConfig) -> bytes:
    return canonical_json(_normalize(config.source_payload))
