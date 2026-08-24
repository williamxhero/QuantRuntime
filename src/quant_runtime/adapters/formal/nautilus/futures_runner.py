from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

import nautilus_trader
import psutil
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.data.config import DataEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity

from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesDataset,
)
from quant_runtime.artifacts import normalize_decimal, write_json

from .decisions import FormalDecisionRecord, decision_envelope, decision_hash
from .futures_config import (
    FuturesContractSpec,
    FuturesExecutionConfig,
    FuturesStrategyContext,
)
from .native_reports import (
    FormalOutput,
    dataframe_records,
    write_normalized_output,
)
from .reporting_input import extract_reporting_input
from .runner import StrategyContext

FUTURES_VENUE = Venue("XCNFUT")
FUTURES_STREAM_BATCH_BARS = 50_000


def run_futures_engine(
    dataset: CanonicalFuturesDataset,
    config: FuturesExecutionConfig,
    strategy: StrategyContext,
    output: Path,
    *,
    strategy_class: type,
    decision_intents: frozenset[str],
) -> FormalOutput:
    dataset.validate()
    config.validate_dataset(dataset)
    output.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    rss_before = process.memory_info().rss
    instruments = {
        item.instrument: _native_instrument(
            item.instrument,
            config.contracts[item.instrument],
            dataset.instrument_bounds[item.instrument],
            dataset.series_type,
            config.slippage_ticks,
        )
        for item in dataset.instruments
    }
    canonical_by_native = {str(value.id): key for key, value in instruments.items()}
    native_by_canonical = {key: value.id for key, value in instruments.items()}
    types = _bar_types(instruments)
    context = FuturesStrategyContext(
        strategy=strategy,
        dataset=dataset,
        instruments=instruments,
        contract_specs=config.contracts,
        execution=config,
        streaming=True,
    )
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="QUANT-RUNTIME-FUTURES-001",
            logging=LoggingConfig(log_level="ERROR"),
            data_engine=DataEngineConfig(validate_data_sequence=True),
            run_analysis=True,
        )
    )
    try:
        engine.add_venue(
            venue=FUTURES_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(config.initial_cash_cny, CNY)],
            base_currency=CNY,
            fee_model=FuturesFeeModel(config.contracts, instruments),
            bar_execution=False,
            trade_execution=False,
            use_random_ids=False,
            allow_cash_borrowing=False,
        )
        for instrument in instruments.values():
            engine.add_instrument(instrument)
        package_strategy = strategy_class(
            context=context,
            bar_types=types,
            canonical_by_native=canonical_by_native,
            native_by_canonical=native_by_canonical,
        )
        engine.add_strategy(package_strategy)
        injection_seconds = 0.0
        run_seconds = 0.0
        streaming_chunks = 0
        streamed_native_events = 0
        peak_streaming_batch_bars = 0
        for signal_batch in _signal_batches(dataset.bars):
            context.load_signal_batch(signal_batch)
            inject_started = perf_counter()
            native_batch = _native_events(signal_batch, instruments, config)
            engine.add_data(native_batch, validate=False, sort=True)
            injection_seconds += perf_counter() - inject_started
            run_started = perf_counter()
            engine.run(streaming=True)
            run_seconds += perf_counter() - run_started
            engine.clear_data()
            streaming_chunks += 1
            streamed_native_events += len(native_batch)
            peak_streaming_batch_bars = max(peak_streaming_batch_bars, len(signal_batch))
        run_started = perf_counter()
        engine.end()
        run_seconds += perf_counter() - run_started
        observed = tuple(getattr(package_strategy, "runtime_decisions", ()))
        if any(not isinstance(item, FormalDecisionRecord) for item in observed):
            raise ValueError("futures runtime_decisions must contain FormalDecisionRecord values")
        undeclared = {item.intent for item in observed} - decision_intents
        if undeclared:
            raise ValueError(
                f"futures strategy emitted undeclared decision intents: {sorted(undeclared)}"
            )
        envelope = decision_envelope(observed, strategy.identity_hash, generic=True)
        formal_decision_hash = decision_hash(envelope)
        write_json(output / "strategy_decisions.json", envelope)

        result = engine.get_result()
        orders = engine.trader.generate_orders_report()
        fills = engine.trader.generate_fills_report()
        positions = engine.trader.generate_positions_report()
        account = engine.trader.generate_account_report(FUTURES_VENUE)
        orders.to_csv(output / "native_orders.csv")
        fills.to_csv(output / "native_fills.csv")
        positions.to_csv(output / "native_positions.csv")
        account.to_csv(output / "native_account.csv")
        statistics = extract_reporting_input(
            result=result,
            analyzer=engine.portfolio.analyzer,
            account=engine.portfolio.account(FUTURES_VENUE),
        )
        write_json(output / "native_statistics.json", statistics)
        formal = FormalOutput(
            framework_version=nautilus_trader.__version__,
            data_version=dataset.data_version,
            dataset_version=dataset.dataset_version,
            canonical_input_hash=dataset.input_hash,
            strategy_spec_hash=strategy.identity_hash,
            decision_hash=formal_decision_hash,
            decisions=[item.as_dict() for item in observed],
            orders=_strategy_records(package_strategy, "order_records"),
            rejects=_strategy_records(package_strategy, "reject_records"),
            fills=_strategy_records(package_strategy, "fill_records"),
            positions=_strategy_records(package_strategy, "position_records"),
            account_curve=dataframe_records(account),
            fees=_strategy_records(package_strategy, "fee_records"),
            native_statistics=statistics,
            metrics={
                "data_injection_seconds": injection_seconds,
                "engine_run_seconds": run_seconds,
                "native_account_report_rows": len(account),
                "native_fill_report_rows": len(fills),
                "native_order_report_rows": len(orders),
                "native_position_report_rows": len(positions),
                "futures_cost_semantics": "tagged_open_close_today_v1",
                "futures_slippage_semantics": "native_quote_spread_ticks_v1",
                "futures_data_loading": "nautilus_native_streaming_v1",
                "streaming_chunks": streaming_chunks,
                "streamed_native_events": streamed_native_events,
                "peak_streaming_batch_bars": peak_streaming_batch_bars,
                "rss_before_bytes": rss_before,
                "rss_after_bytes": process.memory_info().rss,
                "futures_execution_profile_hash": config.profile_hash,
                "futures_contract_catalog_dataset_version": (
                    dataset.contract_catalog.dataset_version
                    if dataset.contract_catalog is not None
                    else None
                ),
                "futures_contract_catalog_snapshot_id": (
                    dataset.contract_catalog.snapshot_id
                    if dataset.contract_catalog is not None
                    else None
                ),
            },
        )
        write_normalized_output(output, formal)
        return formal
    finally:
        engine.dispose()


class FuturesFeeModel(FeeModel):
    def __init__(
        self,
        specs: dict[str, FuturesContractSpec],
        instruments: dict[str, FuturesContract],
    ) -> None:
        super().__init__()
        self._spec_by_native = {
            str(instruments[canonical].id): spec for canonical, spec in specs.items()
        }

    def get_commission(self, order, fill_qty, fill_px, instrument) -> Money:
        spec = self._spec_by_native[str(instrument.id)]
        action = _commission_action(order, spec)
        commission_spec = spec.commission[action]
        quantity = fill_qty.as_decimal()
        notional = quantity * fill_px.as_decimal() * spec.multiplier
        commission = quantity * commission_spec.per_contract + notional * commission_spec.rate
        return Money(commission, CNY)


def _native_instrument(
    canonical: str,
    spec: FuturesContractSpec,
    bounds: tuple[Any, Any],
    series_type: str,
    slippage_ticks: Decimal,
) -> FuturesContract:
    first, last = bounds
    instrument_id = InstrumentId(Symbol(canonical), FUTURES_VENUE)
    native_precision = _native_precision(spec, slippage_ticks)
    return FuturesContract(
        instrument_id=instrument_id,
        raw_symbol=Symbol(canonical),
        asset_class=AssetClass[spec.asset_class],
        currency=CNY,
        price_precision=native_precision,
        price_increment=Price.from_str(f"{spec.tick_size:.{native_precision}f}"),
        multiplier=Quantity.from_str(normalize_decimal(spec.multiplier)),
        lot_size=Quantity.from_str(normalize_decimal(spec.lot_size)),
        underlying=spec.product_code,
        activation_ns=max(0, dt_to_unix_nanos(first) - 1),
        expiration_ns=dt_to_unix_nanos(last) + 86_400_000_000_000,
        ts_event=0,
        ts_init=0,
        margin_init=spec.margin_init,
        margin_maint=spec.margin_maint,
        exchange=spec.exchange,
        info={
            "canonical_instrument": canonical,
            "product_code": spec.product_code,
            "series_type": series_type,
            "configured_price_precision": spec.price_precision,
        },
    )


def _native_precision(spec: FuturesContractSpec, slippage_ticks: Decimal) -> int:
    # A fractional-tick slippage policy may need more precision than the exchange quote.
    # The native price increment remains exactly the frozen tick value.
    slippage_precision = max(0, -(spec.tick_size * slippage_ticks).as_tuple().exponent)
    return max(spec.price_precision, slippage_precision)


def _bar_types(instruments: dict[str, FuturesContract]) -> tuple[BarType, ...]:
    return tuple(
        BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
        for instrument in instruments.values()
    )


def _native_events(
    bars: list[CanonicalFuturesBar],
    instruments: dict[str, FuturesContract],
    config: FuturesExecutionConfig,
) -> list[QuoteTick | Bar]:
    result: list[QuoteTick | Bar] = []
    types = {
        key: BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
        for key, instrument in instruments.items()
    }
    size = Quantity.from_int(1_000_000_000)
    for item in bars:
        instrument = instruments[item.instrument]
        spec = config.contracts[item.instrument]
        slippage = spec.tick_size * config.slippage_ticks
        timestamp = dt_to_unix_nanos(item.bar_time)
        result.append(
            QuoteTick(
                instrument_id=instrument.id,
                bid_price=instrument.make_price(item.economic_close - slippage),
                ask_price=instrument.make_price(item.economic_close + slippage),
                bid_size=size,
                ask_size=size,
                ts_event=timestamp,
                ts_init=timestamp,
            )
        )
        result.append(
            Bar(
                bar_type=types[item.instrument],
                open=instrument.make_price(item.economic_open),
                high=instrument.make_price(item.economic_high),
                low=instrument.make_price(item.economic_low),
                close=instrument.make_price(item.economic_close),
                volume=Quantity.from_str(normalize_decimal(item.volume)),
                ts_event=timestamp,
                ts_init=timestamp,
            )
        )
    return result


def _signal_batches(bars, *, batch_size: int = FUTURES_STREAM_BATCH_BARS):
    batch: list[CanonicalFuturesBar] = []
    previous_timestamp = None
    for bar in bars:
        if batch and len(batch) >= batch_size and bar.bar_time != previous_timestamp:
            yield batch
            batch = []
        batch.append(bar)
        previous_timestamp = bar.bar_time
    if batch:
        yield batch


def _strategy_records(strategy: Any, name: str) -> list[dict[str, Any]]:
    value = getattr(strategy, name, ())
    if not isinstance(value, list | tuple) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"futures strategy {name} must be a list of objects")
    return list(value)


def _commission_action(order, spec: FuturesContractSpec) -> str:
    prefix = "commission:"
    actions = [
        str(tag).removeprefix(prefix) for tag in (order.tags or ()) if str(tag).startswith(prefix)
    ]
    if len(actions) > 1 or any(action not in spec.commission for action in actions):
        raise ValueError("futures order has invalid or conflicting commission action tags")
    if actions:
        return actions[0]
    if spec.requires_commission_tag:
        raise ValueError(f"futures order for {spec.instrument!r} requires a commission action tag")
    return "open"
