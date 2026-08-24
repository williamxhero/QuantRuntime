from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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

from quant_runtime.adapters.data.markethub.model import CanonicalDataset
from quant_runtime.artifacts import sha256_value, write_json

from .china_market_rules import AShareFeeModel, AShareRuleBook, FeeSpec
from .decisions import decision_envelope, decision_hash
from .instruments import VENUE, native_instrument
from .native_reports import (
    FormalOutput,
    dataframe_records,
    normalize_value,
    write_normalized_output,
)
from .nautilus_data import bar_types, native_bars, native_quotes

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
class StrategyContext:
    strategy_id: str
    revision: int
    package_hash: str
    parameters_hash: str
    parameters: dict[str, Any]

    @property
    def identity_hash(self) -> str:
        return sha256_value(
            {
                "strategy_id": self.strategy_id,
                "revision": self.revision,
                "package_hash": self.package_hash,
                "parameters_hash": self.parameters_hash,
            }
        )

    def __getattr__(self, name: str) -> Any:
        try:
            return self.parameters[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@dataclass(frozen=True, slots=True)
class FormalConfig:
    strategy: StrategyContext
    initial_cash_cny: Decimal
    lot_size: int
    tick_size: Decimal
    slippage_bps: Decimal
    fees: FeeSpec

    def validate(self, instrument_count: int) -> None:
        if int(self.strategy.parameters.get("top_k", 1)) > instrument_count:
            raise ValueError("top_k exceeds formal universe")
        if self.initial_cash_cny <= 0 or self.lot_size != 100 or self.tick_size != Decimal("0.01"):
            raise ValueError(
                "formal A-share execution requires cash, 100-share lots, and 0.01 tick"
            )
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        self.fees.validate()


def run_engine(
    dataset: CanonicalDataset,
    config: FormalConfig,
    output: Path,
    *,
    strategy_class: type,
) -> FormalOutput:
    dataset.validate()
    config.validate(len(dataset.instruments))
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
        strategy = strategy_class(
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
        envelope = decision_envelope(strategy.runtime_decisions, config.strategy.identity_hash)
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
            strategy_spec_hash=config.strategy.identity_hash,
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
