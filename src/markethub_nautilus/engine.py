from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import nautilus_trader
import psutil
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.data.config import DataEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity

from .canonical import CanonicalDataset, canonical_json, normalize_decimal
from .china_rules import AShareFeeModel, AShareRuleBook
from .config import RunConfig
from .evidence import NormalizedOutput, normalize_value
from .momentum import MomentumReference
from .momentum_strategy import MomentumTopKStrategy
from .strategy import DecisionStrategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
VENUE = Venue("XCN")


def native_instrument(item) -> Equity:
    symbol = Symbol(item.instrument.replace(".", "-"))
    return Equity(
        instrument_id=InstrumentId(symbol=symbol, venue=VENUE),
        raw_symbol=symbol,
        currency=CNY,
        price_precision=item.price_precision,
        price_increment=Price.from_str(normalize_decimal(item.tick_size)),
        lot_size=Quantity.from_int(item.lot_size),
        isin=None,
        ts_event=0,
        ts_init=0,
        info={"canonical_instrument": item.instrument, "exchange": item.exchange},
    )


def run_engine(dataset: CanonicalDataset, config: RunConfig, output_dir: Path) -> NormalizedOutput:
    dataset.validate()
    output_dir.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    rss_before = process.memory_info().rss
    native_by_canonical = {item.instrument: native_instrument(item) for item in dataset.instruments}
    canonical_by_native = {str(value.id): key for key, value in native_by_canonical.items()}
    bars_by_instrument = _native_bars(dataset, native_by_canonical)
    quotes_by_instrument = _native_quotes(dataset, native_by_canonical)
    bar_types = tuple(
        BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        for instrument in native_by_canonical.values()
    )
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="MARKETHUB-001",
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
            starting_balances=[Money(config.strategy.initial_cash_cny, CNY)],
            base_currency=CNY,
            fee_model=AShareFeeModel(config.strategy.fees),
            bar_execution=False,
            use_random_ids=False,
            allow_cash_borrowing=False,
        )
        for instrument in native_by_canonical.values():
            engine.add_instrument(instrument)
        inject_started = perf_counter()
        for quotes in quotes_by_instrument.values():
            engine.add_data(quotes, sort=False)
        for bars in bars_by_instrument.values():
            engine.add_data(bars, sort=False)
        engine.sort_data()
        injection_seconds = perf_counter() - inject_started
        native_ids = {key: value.id for key, value in native_by_canonical.items()}
        rule_book = AShareRuleBook(dataset, config.strategy.rule_overrides)
        is_momentum = config.strategy.kind == "cross_sectional_momentum_topk"
        if is_momentum:
            strategy = MomentumTopKStrategy(
                config.strategy,
                dataset.trading_days,
                bar_types,
                canonical_by_native,
                native_ids,
                rule_book,
                frozenset(
                    (bar.trading_day, bar.instrument) for bar in dataset.bars if bar.is_suspended
                ),
            )
        else:
            strategy = DecisionStrategy(
                config.strategy,
                bar_types,
                dataset.trading_days,
                canonical_by_native,
                native_ids,
                rule_book,
            )
        engine.add_strategy(strategy)
        run_started = perf_counter()
        engine.run()
        run_seconds = perf_counter() - run_started
        if is_momentum:
            runtime_reference = MomentumReference(
                strategy_spec_hash=config.strategy.spec_hash,
                decisions=tuple(strategy.runtime_decisions),
            )
            decision_hash = runtime_reference.decision_hash
            (output_dir / "strategy_decisions.json").write_bytes(
                canonical_json(runtime_reference.envelope()) + b"\n"
            )
        else:
            decision_hash = config.strategy.decision_hash
        result = engine.get_result()
        orders = engine.trader.generate_orders_report()
        fills = engine.trader.generate_fills_report()
        positions = engine.trader.generate_positions_report()
        account = engine.trader.generate_account_report(VENUE)
        orders.to_csv(output_dir / "native_orders.csv")
        fills.to_csv(output_dir / "native_fills.csv")
        positions.to_csv(output_dir / "native_positions.csv")
        account.to_csv(output_dir / "native_account.csv")
        native_statistics = normalize_value(
            {
                "stats_pnls": result.stats_pnls,
                "stats_returns": result.stats_returns,
                "summary": result.summary,
                "total_events": result.total_events,
                "total_orders": result.total_orders,
                "total_positions": result.total_positions,
            }
        )
        _write_json(output_dir / "native_statistics.json", native_statistics)
        output = NormalizedOutput(
            framework_version=nautilus_trader.__version__,
            data_version=dataset.data_version,
            canonical_input_hash=dataset.input_hash,
            strategy_spec_hash=config.strategy.spec_hash,
            decision_hash=decision_hash,
            decisions=strategy.decision_records,
            orders=strategy.order_records,
            rejects=strategy.reject_records,
            fills=strategy.fill_records,
            positions=strategy.position_records,
            account_curve=_dataframe_records(account),
            fees=strategy.fee_records,
            native_statistics=native_statistics,
            metrics={
                "data_injection_seconds": injection_seconds,
                "engine_run_seconds": run_seconds,
                "native_account_report_rows": len(account),
                "native_fill_report_rows": len(fills),
                "native_order_report_rows": len(orders),
                "native_position_report_rows": len(positions),
                "rss_after_bytes": process.memory_info().rss,
                "rss_before_bytes": rss_before,
            },
        )
        _write_json(
            output_dir / "normalized_output.json",
            {**output.semantic_payload(), "normalized_output_hash": output.output_hash},
        )
        return output
    finally:
        engine.dispose()


def _native_bars(
    dataset: CanonicalDataset,
    instruments: dict[str, Equity],
) -> dict[str, list[Bar]]:
    result: dict[str, list[Bar]] = {key: [] for key in instruments}
    bar_types = {
        key: BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
        for key, instrument in instruments.items()
    }
    for item in dataset.bars:
        instrument = instruments[item.instrument]
        open_price, high, low, close = _safe_prices(item)
        timestamp = dt_to_unix_nanos(datetime.combine(item.trading_day, time(15), tzinfo=SHANGHAI))
        result[item.instrument].append(
            Bar(
                bar_type=bar_types[item.instrument],
                open=instrument.make_price(open_price),
                high=instrument.make_price(high),
                low=instrument.make_price(low),
                close=instrument.make_price(close),
                volume=Quantity.from_str(normalize_decimal(item.volume)),
                ts_event=timestamp,
                ts_init=timestamp,
            )
        )
    return result


def _native_quotes(
    dataset: CanonicalDataset,
    instruments: dict[str, Equity],
) -> dict[str, list[QuoteTick]]:
    result: dict[str, list[QuoteTick]] = {key: [] for key in instruments}
    size = Quantity.from_int(1_000_000_000)
    for item in dataset.bars:
        instrument = instruments[item.instrument]
        open_price, _, _, close = _safe_prices(item)
        for at, value in ((time(9, 30), open_price), (time(14, 59, 59, 999999), close)):
            timestamp = dt_to_unix_nanos(datetime.combine(item.trading_day, at, tzinfo=SHANGHAI))
            price = instrument.make_price(value)
            result[item.instrument].append(
                QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=price,
                    ask_price=price,
                    bid_size=size,
                    ask_size=size,
                    ts_event=timestamp,
                    ts_init=timestamp,
                )
            )
    return result


def _safe_prices(item) -> tuple[object, object, object, object]:
    if item.is_suspended and min(item.open, item.high, item.low, item.close) <= 0:
        return item.pre_close, item.pre_close, item.pre_close, item.pre_close
    return item.open, item.high, item.low, item.close


def _dataframe_records(frame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    reset = frame.reset_index()
    return json.loads(reset.to_json(orient="records", date_format="iso"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
