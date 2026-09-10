from __future__ import annotations

from decimal import Decimal

import nautilus_trader

from quant_runtime.adapters.data.markethub.cache import MarketHubCache
from quant_runtime.adapters.data.markethub.futures_model import CanonicalFuturesDataset
from quant_runtime.adapters.formal.nautilus.china_market_rules import FeeSpec
from quant_runtime.adapters.formal.nautilus.futures_config import FuturesExecutionConfig
from quant_runtime.adapters.formal.nautilus.futures_runner import run_futures_engine
from quant_runtime.adapters.formal.nautilus.runner import (
    BASE_ARTIFACTS,
    FormalConfig,
    StrategyContext,
    run_engine,
)
from quant_runtime.adapters.interface import FormalAdapterResult, FormalRunInput
from quant_runtime.artifacts import artifact_records
from quant_runtime.entrypoint import load_package_entrypoint

ADAPTER_VERSION = "1.2.0"
OPERATIONAL_METRICS = frozenset(
    {"data_injection_seconds", "engine_run_seconds", "rss_before_bytes", "rss_after_bytes"}
)


class NautilusStrategyError(RuntimeError):
    """A package-owned formal entrypoint could not be accepted by Runtime."""


class NautilusWorkspaceAdapter:
    name = "nautilus"
    adapter_version = ADAPTER_VERSION
    engine_version = nautilus_trader.__version__

    def run(self, value: FormalRunInput, *, formal_id: str) -> FormalAdapterResult:
        if value.snapshot.dataset is None:
            raise ValueError("Nautilus formal execution requires a verified snapshot read")
        dataset = value.snapshot.dataset
        cache_consumed = False
        read_method = (
            "materialized_parquet" if value.snapshot.mode == "materialized" else "direct_markethub"
        )
        if value.cache_path is not None:
            dataset = MarketHubCache.load(value.cache_path)
            if dataset.input_hash != value.snapshot.dataset.input_hash:
                raise ValueError("formal cache input differs from the verified snapshot input")
            cache_consumed = True
            read_method = "non_authoritative_cache"
        try:
            strategy_class = load_package_entrypoint(
                value.package.root,
                value.package.resolve_entrypoint("formal", self.name),
            )
        except Exception as exc:
            raise NautilusStrategyError("Nautilus strategy entrypoint was rejected") from exc
        config = _formal_config(value)
        if isinstance(dataset, CanonicalFuturesDataset):
            result = run_futures_engine(
                dataset,
                _futures_config(value),
                config.strategy,
                value.output,
                strategy_class=strategy_class,
                decision_intents=value.package.decision_intents,
            )
        else:
            if value.package.asset_classes != frozenset(
                {"equity"}
            ) or value.package.frequencies != frozenset({"1d"}):
                raise ValueError("daily equity snapshot requires an equity/1d strategy package")
            result = run_engine(
                dataset,
                config,
                value.output,
                strategy_class=strategy_class,
            )
        paths = [value.output / name for name in BASE_ARTIFACTS]
        partial_lineage = value.output / "partial_snapshot_lineage.json"
        if partial_lineage.exists():
            paths.append(partial_lineage)
        partial_stream_verification = value.output / "partial_stream_verification.json"
        if partial_stream_verification.exists():
            paths.append(partial_stream_verification)
        evidence = tuple(artifact_records(value.output, paths))
        return FormalAdapterResult(
            formal_id=formal_id,
            backend_id=self.name,
            adapter_version=self.adapter_version,
            engine_version=self.engine_version,
            status="completed",
            metrics={
                **{
                    key: item
                    for key, item in result.metrics.items()
                    if key not in OPERATIONAL_METRICS
                },
                "strategy_package_hash": value.package.package_hash,
                "parameters_hash": value.package.parameters_hash(value.parameters),
                "snapshot_id": value.snapshot.snapshot_id,
                "formal_decision_hash": result.decision_hash,
                "normalized_output_hash": result.output_hash,
                "cache_policy": value.cache_policy,
                "cache_transform_version": value.cache_transform_version,
                "cache_consumed": cache_consumed,
                "read_method": read_method,
            },
            positions=tuple(result.positions),
            fills=tuple(result.fills),
            account_curve=tuple(result.account_curve),
            native_evidence=evidence,
        )


def _formal_config(value: FormalRunInput) -> FormalConfig:
    execution = value.config.get("execution", value.config)
    if not isinstance(execution, dict):
        raise ValueError("formal config execution must be an object")
    fee = execution.get("fees", {})
    if not isinstance(fee, dict):
        raise ValueError("formal config fees must be an object")
    config = FormalConfig(
        strategy=StrategyContext(
            strategy_id=value.package.strategy_id,
            revision=value.package.revision,
            package_hash=value.package.package_hash,
            parameters_hash=value.package.parameters_hash(value.parameters),
            parameters=value.parameters,
        ),
        initial_cash_cny=Decimal(str(execution.get("initial_cash_cny", "1000000.00"))),
        lot_size=int(execution.get("lot_size", 100)),
        tick_size=Decimal(str(execution.get("tick_size", "0.01"))),
        slippage_bps=Decimal(str(execution.get("slippage_bps", "0"))),
        fees=FeeSpec(
            commission_rate=Decimal(str(fee.get("commission_rate", "0.0003"))),
            minimum_commission_cny=Decimal(str(fee.get("minimum_commission_cny", "5.00"))),
            sell_stamp_duty_rate=Decimal(str(fee.get("sell_stamp_duty_rate", "0.0005"))),
            currency_precision=int(fee.get("currency_precision", 2)),
            rounding_mode=str(fee.get("rounding_mode", "half_away_from_zero")),
            rounding_scope=str(fee.get("rounding_scope", "per_fill")),
        ),
    )
    return config


def _futures_config(value: FormalRunInput) -> FuturesExecutionConfig:
    if value.package.asset_classes != frozenset({"futures"}):
        raise ValueError("futures snapshot requires package asset_classes=['futures']")
    if value.package.frequencies != frozenset({"1m"}):
        raise ValueError("futures snapshot requires package frequencies=['1m']")
    execution = value.config.get("execution", value.config)
    if not isinstance(execution, dict):
        raise ValueError("formal config execution must be an object")
    return FuturesExecutionConfig.from_dict(execution)
