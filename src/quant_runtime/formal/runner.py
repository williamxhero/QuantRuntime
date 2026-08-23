from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

import nautilus_trader
import psutil
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.data.config import DataEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

from quant_runtime.contracts.candidate_manifest import CandidateManifest
from quant_runtime.contracts.canonical_hash import (
    artifact_records,
    read_json,
    sha256_bytes,
    sha256_value,
    write_json,
)
from quant_runtime.contracts.formal_manifest import FORMAL_SCHEMA
from quant_runtime.contracts.strategy_spec import StrategySpec, resolve_strategy_path
from quant_runtime.markethub.client import MarketHubClient
from quant_runtime.markethub.daily_data import CanonicalDataset
from quant_runtime.semantics.decision_record import decision_envelope, decision_hash

from .china_market_rules import AShareFeeModel, AShareRuleBook, FeeSpec
from .instruments import VENUE, native_instrument
from .native_reports import (
    FormalOutput,
    dataframe_records,
    normalize_value,
    write_normalized_output,
)
from .nautilus_data import bar_types, native_bars, native_quotes
from .strategies import MomentumTopKStrategy

BASE_ARTIFACTS = (
    "native_account.csv",
    "native_fills.csv",
    "native_orders.csv",
    "native_positions.csv",
    "native_statistics.json",
    "normalized_output.json",
    "strategy_decisions.json",
)


@dataclass(frozen=True, slots=True)
class FormalConfig:
    path: Path
    source_bytes: bytes
    strategy: StrategySpec
    base_url: str
    timeout_seconds: float
    page_size: int
    instruments: tuple[str, ...]
    start_date: date
    end_date: date
    initial_cash_cny: Decimal
    lot_size: int
    tick_size: Decimal
    slippage_bps: Decimal
    fees: FeeSpec

    @classmethod
    def load(cls, path: Path) -> FormalConfig:
        source_bytes = path.read_bytes()
        raw = read_json(path)
        if raw.get("schema") != "quant-runtime.formal-config.v1":
            raise ValueError("unsupported formal config schema")
        market_hub = _object(raw, "market_hub")
        universe = _object(raw, "universe")
        execution = _object(raw, "execution")
        fee = _object(execution, "fees")
        config = cls(
            path=path.resolve(),
            source_bytes=source_bytes,
            strategy=StrategySpec.load(resolve_strategy_path(path, raw.get("strategy_spec"))),
            base_url=str(market_hub.get("base_url", "")).rstrip("/"),
            timeout_seconds=float(market_hub.get("timeout_seconds", 60)),
            page_size=int(market_hub.get("page_size", 50_000)),
            instruments=tuple(str(item) for item in universe.get("instruments", [])),
            start_date=date.fromisoformat(str(raw.get("start_date", ""))),
            end_date=date.fromisoformat(str(raw.get("end_date", ""))),
            initial_cash_cny=Decimal(str(execution.get("initial_cash_cny", "0"))),
            lot_size=int(execution.get("lot_size", 0)),
            tick_size=Decimal(str(execution.get("tick_size", "0"))),
            slippage_bps=Decimal(str(execution.get("slippage_bps", "0"))),
            fees=FeeSpec(
                commission_rate=Decimal(str(fee.get("commission_rate", "0"))),
                minimum_commission_cny=Decimal(str(fee.get("minimum_commission_cny", "0"))),
                sell_stamp_duty_rate=Decimal(str(fee.get("sell_stamp_duty_rate", "0"))),
                currency_precision=int(fee.get("currency_precision", 0)),
                rounding_mode=str(fee.get("rounding_mode", "")),
                rounding_scope=str(fee.get("rounding_scope", "")),
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.base_url or self.timeout_seconds <= 0 or not 1 <= self.page_size <= 100_000:
            raise ValueError("invalid MarketHub formal settings")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        if self.instruments != tuple(sorted(set(self.instruments))) or not self.instruments:
            raise ValueError("formal instruments must be unique and canonical-order sorted")
        if self.strategy.top_k > len(self.instruments):
            raise ValueError("top_k exceeds formal universe")
        if self.initial_cash_cny <= 0 or self.lot_size != 100 or self.tick_size != Decimal("0.01"):
            raise ValueError(
                "formal A-share execution requires cash, 100-share lots, and 0.01 tick"
            )
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        self.fees.validate()

    @property
    def config_hash(self) -> str:
        return sha256(self.source_bytes).hexdigest()


def evaluate_candidate(
    candidate_path: Path,
    config_path: Path,
    output: Path,
) -> tuple[dict[str, Any], Path]:
    candidate = CandidateManifest.load(candidate_path)
    config = FormalConfig.load(config_path)
    if candidate.strategy_spec_hash != config.strategy.spec_hash:
        raise ValueError("candidate strategy_spec_hash does not match formal config")
    client = MarketHubClient(config.base_url, timeout_seconds=config.timeout_seconds)
    dataset = client.fetch_dataset(
        config.instruments,
        config.start_date,
        config.end_date,
        page_size=config.page_size,
    )
    if candidate.data_version != dataset.data_version:
        raise ValueError("candidate data_version does not match the formal MarketHub snapshot")
    result = run_engine(dataset, config, output)
    matches = {
        "data_version_match": candidate.data_version == result.data_version,
        "dataset_version_match": candidate.dataset_version == result.dataset_version,
        "strategy_spec_match": candidate.strategy_spec_hash == result.strategy_spec_hash,
        "canonical_input_match": candidate.canonical_input_hash == result.canonical_input_hash,
        "decision_match": candidate.reference_decision_hash == result.decision_hash,
    }
    semantic_match = all(matches.values())
    candidate_hash = sha256_bytes(candidate.path.read_bytes())
    run_id = (
        "qr-formal-"
        + sha256_value(
            {
                "candidate_run_id": candidate.run_id,
                "config_hash": config.config_hash,
                "canonical_input_hash": dataset.input_hash,
                "normalized_output_hash": result.output_hash,
            }
        )[:24]
    )
    metrics = {
        **matches,
        "semantic_match": semantic_match,
        "candidate_decision_hash": candidate.reference_decision_hash,
        "formal_decision_hash": result.decision_hash,
        "fetch": client.metrics.as_dict(),
        "runtime": result.metrics,
    }
    manifest = {
        "schema": FORMAL_SCHEMA,
        "run_id": run_id,
        "framework": "NautilusTrader",
        "framework_version": nautilus_trader.__version__,
        "status": "matched" if semantic_match else "mismatched",
        "data_version": dataset.data_version,
        "dataset_version": dataset.dataset_version,
        "config_hash": config.config_hash,
        "strategy_spec_hash": config.strategy.spec_hash,
        "canonical_input_hash": dataset.input_hash,
        "normalized_output_hash": result.output_hash,
        "candidate_run_id": candidate.run_id,
        "candidate_manifest_hash": candidate_hash,
        "artifacts": artifact_records(output, [output / name for name in BASE_ARTIFACTS]),
        "metrics": metrics,
    }
    path = write_json(output / "formal_manifest.json", manifest).resolve()
    return manifest, path


def run_engine(
    dataset: CanonicalDataset,
    config: FormalConfig,
    output: Path,
) -> FormalOutput:
    dataset.validate()
    output.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    rss_before = process.memory_info().rss
    instruments = {item.instrument: native_instrument(item) for item in dataset.instruments}
    canonical_by_native = {str(value.id): key for key, value in instruments.items()}
    types = bar_types(instruments)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="QUANT-RUNTIME-001",
            logging=LoggingConfig(log_level="ERROR"),
            data_engine=DataEngineConfig(validate_data_sequence=True),
            run_analysis=True,
        )
    )
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money(config.initial_cash_cny, CNY)],
            base_currency=CNY,
            fee_model=AShareFeeModel(config.fees),
            bar_execution=False,
            use_random_ids=False,
            allow_cash_borrowing=False,
        )
        for instrument in instruments.values():
            engine.add_instrument(instrument)
        inject_started = perf_counter()
        for quotes in native_quotes(dataset, instruments, config.slippage_bps).values():
            engine.add_data(quotes, sort=False)
        for bars in native_bars(dataset, instruments).values():
            engine.add_data(bars, sort=False)
        engine.sort_data()
        injection_seconds = perf_counter() - inject_started
        strategy = MomentumTopKStrategy(
            config.strategy,
            config.fees,
            config.lot_size,
            dataset.trading_days,
            types,
            canonical_by_native,
            {key: value.id for key, value in instruments.items()},
            AShareRuleBook(dataset),
            frozenset(
                (bar.trading_day, bar.instrument) for bar in dataset.bars if bar.is_suspended
            ),
        )
        engine.add_strategy(strategy)
        run_started = perf_counter()
        engine.run()
        run_seconds = perf_counter() - run_started
        envelope = decision_envelope(strategy.runtime_decisions, config.strategy.spec_hash)
        formal_decision_hash = decision_hash(envelope)
        write_json(output / "strategy_decisions.json", envelope)
        result = engine.get_result()
        orders = engine.trader.generate_orders_report()
        fills = engine.trader.generate_fills_report()
        positions = engine.trader.generate_positions_report()
        account = engine.trader.generate_account_report(VENUE)
        orders.to_csv(output / "native_orders.csv")
        fills.to_csv(output / "native_fills.csv")
        positions.to_csv(output / "native_positions.csv")
        account.to_csv(output / "native_account.csv")
        statistics = normalize_value(
            {
                "stats_pnls": result.stats_pnls,
                "stats_returns": result.stats_returns,
                "summary": result.summary,
                "total_events": result.total_events,
                "total_orders": result.total_orders,
                "total_positions": result.total_positions,
            }
        )
        write_json(output / "native_statistics.json", statistics)
        formal = FormalOutput(
            framework_version=nautilus_trader.__version__,
            data_version=dataset.data_version,
            dataset_version=dataset.dataset_version,
            canonical_input_hash=dataset.input_hash,
            strategy_spec_hash=config.strategy.spec_hash,
            decision_hash=formal_decision_hash,
            decisions=strategy.decision_records,
            orders=strategy.order_records,
            rejects=strategy.reject_records,
            fills=strategy.fill_records,
            positions=strategy.position_records,
            account_curve=dataframe_records(account),
            fees=strategy.fee_records,
            native_statistics=statistics,
            metrics={
                "data_injection_seconds": injection_seconds,
                "engine_run_seconds": run_seconds,
                "native_account_report_rows": len(account),
                "native_fill_report_rows": len(fills),
                "native_order_report_rows": len(orders),
                "native_position_report_rows": len(positions),
                "rss_before_bytes": rss_before,
                "rss_after_bytes": process.memory_info().rss,
            },
        )
        write_normalized_output(output, formal)
        return formal
    finally:
        engine.dispose()


def _object(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
