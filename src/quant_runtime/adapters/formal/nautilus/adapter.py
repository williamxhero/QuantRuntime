from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import nautilus_trader

from quant_runtime.adapters.data.markethub.cache import MarketHubCache
from quant_runtime.adapters.data.markethub.futures_model import CanonicalFuturesDataset
from quant_runtime.adapters.formal.nautilus.china_market_rules import FeeSpec
from quant_runtime.adapters.formal.nautilus.cost_receipt import (
    EffectiveCostConfig,
    build_cost_receipt,
)
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

ADAPTER_VERSION = "1.3.0"
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
        if isinstance(dataset, CanonicalFuturesDataset):
            futures_config = _futures_config(value)
            result = run_futures_engine(
                dataset,
                futures_config,
                StrategyContext(
                    strategy_id=value.package.strategy_id,
                    revision=value.package.revision,
                    package_hash=value.package.package_hash,
                    parameters_hash=value.package.parameters_hash(value.parameters),
                    parameters=value.parameters,
                ),
                value.output,
                strategy_class=strategy_class,
                decision_intents=value.package.decision_intents,
            )
            cost_receipt = None
        else:
            if value.package.asset_classes != frozenset(
                {"equity"}
            ) or value.package.frequencies != frozenset({"1d"}):
                raise ValueError("daily equity snapshot requires an equity/1d strategy package")
            config = _formal_config(
                value,
                allow_legacy_metadata=value.request_schema
                == "quant-research.workspace-run-request.v2",
            )
            config.validate(len(dataset.instruments))
            cost_receipt = _build_cost_receipt(value, config, formal_id)
            result = run_engine(
                dataset,
                config,
                value.output,
                strategy_class=strategy_class,
                cost_receipt=cost_receipt,
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
            cost_receipt=cost_receipt,
        )


def _formal_config(value: FormalRunInput, *, allow_legacy_metadata: bool = False) -> FormalConfig:
    execution = _execution_object(value.config)
    allowed = {
        "fees",
        "initial_cash_cny",
        "lot_size",
        "market_data",
        "slippage_bps",
        "tick_size",
    }
    if allow_legacy_metadata:
        allowed.add("score")
    unknown = set(execution) - allowed
    if unknown:
        raise ValueError(f"formal config contains unknown fields: {sorted(unknown)}")
    fee = execution.get("fees", {})
    if not isinstance(fee, Mapping):
        raise ValueError("formal config fees must be an object")
    fee = dict(fee)
    fee_allowed = {
        "commission_rate",
        "currency_precision",
        "minimum_commission_cny",
        "rounding_mode",
        "rounding_scope",
        "sell_stamp_duty_rate",
    }
    if set(fee) - fee_allowed:
        raise ValueError(f"formal fees contain unknown fields: {sorted(set(fee) - fee_allowed)}")
    market_data = execution.get("market_data")
    if market_data is not None:
        if not isinstance(market_data, Mapping) or set(market_data) != {"local_cache"}:
            raise ValueError("formal market_data must contain only local_cache")
        if market_data["local_cache"] not in {"none", "ephemeral"}:
            raise ValueError("formal market_data.local_cache is invalid")
    config = FormalConfig(
        strategy=StrategyContext(
            strategy_id=value.package.strategy_id,
            revision=value.package.revision,
            package_hash=value.package.package_hash,
            parameters_hash=value.package.parameters_hash(value.parameters),
            parameters=value.parameters,
        ),
        initial_cash_cny=_decimal_value(
            execution.get("initial_cash_cny", "1000000.00"), "initial_cash_cny"
        ),
        lot_size=_integer_value(execution.get("lot_size", 100), "lot_size"),
        tick_size=_decimal_value(execution.get("tick_size", "0.01"), "tick_size"),
        slippage_bps=_decimal_value(execution.get("slippage_bps", "0"), "slippage_bps"),
        fees=FeeSpec(
            commission_rate=_decimal_value(fee.get("commission_rate", "0.0003"), "commission_rate"),
            minimum_commission_cny=_decimal_value(
                fee.get("minimum_commission_cny", "5.00"), "minimum_commission_cny"
            ),
            sell_stamp_duty_rate=_decimal_value(
                fee.get("sell_stamp_duty_rate", "0.0005"), "sell_stamp_duty_rate"
            ),
            currency_precision=_integer_value(
                fee.get("currency_precision", 2), "currency_precision"
            ),
            rounding_mode=_string_value(
                fee.get("rounding_mode", "half_away_from_zero"), "rounding_mode"
            ),
            rounding_scope=_string_value(fee.get("rounding_scope", "per_fill"), "rounding_scope"),
        ),
    )
    if (
        config.initial_cash_cny <= 0
        or config.lot_size != 100
        or config.tick_size != Decimal("0.01")
    ):
        raise ValueError("formal A-share execution requires cash, 100-share lots, and 0.01 tick")
    if config.slippage_bps < 0:
        raise ValueError("slippage_bps must be non-negative")
    config.fees.validate()
    return config


def _build_cost_receipt(
    value: FormalRunInput, config: FormalConfig, formal_id: str
) -> dict[str, Any] | None:
    if value.request_hash is None and value.runtime_identity is None:
        # Direct adapter callers predate public receipt publication and do not have
        # an immutable Workspace binding to attach to a local result.
        return None
    if not value.request_hash:
        raise ValueError("formal cost receipt requires the immutable request hash")
    runtime = value.runtime_identity
    if not isinstance(runtime, Mapping):
        raise ValueError("formal cost receipt requires runtime image and lock identity")
    return build_cost_receipt(
        EffectiveCostConfig.from_formal_config(config),
        request_hash=value.request_hash,
        package_hash=value.package.package_hash,
        formal_phase_id=formal_id,
        runtime_image_identity=str(runtime.get("image_identity", "")),
        runtime_lock_identity=str(runtime.get("lock_identity", "")),
    )


def _execution_object(value: Mapping[str, Any]) -> dict[str, Any]:
    if "execution" in value:
        if set(value) != {"execution"} or not isinstance(value["execution"], Mapping):
            raise ValueError("formal config execution must be an object")
        return dict(value["execution"])
    return dict(value)


def _decimal_value(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"formal config {name} must be a decimal")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"formal config {name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"formal config {name} must be finite")
    return result


def _integer_value(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"formal config {name} must be an integer")
    return int(value)


def _string_value(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"formal config {name} must be a non-empty string")
    return value


def _futures_config(value: FormalRunInput) -> FuturesExecutionConfig:
    if value.package.asset_classes != frozenset({"futures"}):
        raise ValueError("futures snapshot requires package asset_classes=['futures']")
    if value.package.frequencies != frozenset({"1m"}):
        raise ValueError("futures snapshot requires package frequencies=['1m']")
    execution = value.config.get("execution", value.config)
    if not isinstance(execution, dict):
        raise ValueError("formal config execution must be an object")
    return FuturesExecutionConfig.from_dict(execution)
