from __future__ import annotations

from datetime import date
from decimal import Decimal

import nautilus_trader

from quant_runtime.adapters.data.markethub.cache import MarketHubCache
from quant_runtime.adapters.interface import FormalAdapterResult, FormalRunInput
from quant_runtime.contracts.canonical_hash import artifact_records, canonical_json
from quant_runtime.contracts.strategy_spec import StrategySpec
from quant_runtime.formal.nautilus.china_market_rules import FeeSpec
from quant_runtime.formal.nautilus.runner import BASE_ARTIFACTS, FormalConfig, run_engine
from quant_runtime.sdk.entrypoint import load_package_entrypoint

ADAPTER_VERSION = "1.0.0"


class NautilusWorkspaceAdapter:
    name = "nautilus"
    adapter_version = ADAPTER_VERSION
    engine_version = nautilus_trader.__version__

    def run(self, value: FormalRunInput) -> FormalAdapterResult:
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
        strategy_class = load_package_entrypoint(
            value.package.root,
            value.package.resolve_entrypoint("formal", self.name),
        )
        config = _formal_config(value)
        result = run_engine(
            dataset,
            config,
            value.output,
            strategy_class=strategy_class,
        )
        evidence = tuple(
            artifact_records(value.output, [value.output / name for name in BASE_ARTIFACTS])
        )
        return FormalAdapterResult(
            backend_id=self.name,
            adapter_version=self.adapter_version,
            engine_version=self.engine_version,
            status="completed",
            metrics={
                **result.metrics,
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
    source = canonical_json(value.config)
    query = value.snapshot.manifest["query"]
    config = FormalConfig(
        path=(value.package.root / "strategy.toml").resolve(),
        source_bytes=source,
        strategy=StrategySpec.from_parameters(
            value.package.strategy_id,
            value.package.revision,
            value.parameters,
        ),
        base_url=str(value.snapshot.manifest["source"]["base_url"]),
        timeout_seconds=float(execution.get("timeout_seconds", 60.0)),
        page_size=int(execution.get("page_size", 50_000)),
        instruments=tuple(str(item) for item in query["instruments"]),
        start_date=date.fromisoformat(str(query["start"])),
        end_date=date.fromisoformat(str(query["end"])),
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
    config.validate()
    return config
