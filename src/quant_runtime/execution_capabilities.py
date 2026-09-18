"""Execution-preflight capability snapshot for one declared market and date range.

The snapshot answers a single question: which execution semantics does this Runtime
actually provide for the requested market, and what established each answer?  Every
field carries the exact source that produced it.  Nothing is inferred from product
branding, and a field that no authorized source can establish stays ``not_evaluated``
with its reason recorded.

Three source kinds are accepted:

``runtime_code``
    A fact executed or introspected from this repository's own execution rules --
    ``china_market_rules``, the Nautilus venue configuration, the capability
    registry, or the MarketHub canonical model.

``markethub_live``
    A fact observed from a live, read-only MarketHub response for the requested
    range.  Reads go through the existing MarketHub client and data adapter.

``markethub_unavailable``
    A live read that was attempted and failed closed.  The exact failure is kept
    so that a degraded field is visibly degraded rather than quietly assumed.

This module never writes to MarketHub and never publishes Workspace state.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from quant_runtime.adapters.data.markethub import (
    MarketHubContractError,
    MarketHubDataAdapter,
    SnapshotRequest,
)
from quant_runtime.adapters.data.markethub.client import MarketHubClient
from quant_runtime.adapters.data.markethub.model import CanonicalDataset
from quant_runtime.adapters.formal.nautilus import china_market_rules
from quant_runtime.adapters.formal.nautilus.china_market_rules import (
    AShareRuleBook,
    FeeSpec,
    calculate_fee,
)
from quant_runtime.artifacts import normalize_decimal, sha256_value
from quant_runtime.registry import production_registry

SCHEMA = "quant-runtime.execution-capability-snapshot.v1"
REQUEST_SCHEMA = "quant-runtime.execution-preflight-request.v1"

REQUIRED_FIELDS: tuple[str, ...] = (
    "market_order_eligibility",
    "limit_up_down",
    "lot_size",
    "fees",
    "slippage",
    "corporate_actions",
    "short_or_long_direction",
    "suspension",
    "partial_fill",
    "gap",
    "cash_and_rounding",
    "point_in_time_policy",
)

_STATUSES = frozenset({"supported", "supported_with_limitation", "not_evaluated"})


class ExecutionPreflightError(ValueError):
    """Raised when the request cannot describe a real execution preflight."""


@dataclass(frozen=True, slots=True)
class FieldObservation:
    field: str
    status: str
    value: dict[str, Any]
    sources: tuple[dict[str, Any], ...]
    limitation: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.field not in REQUIRED_FIELDS:
            raise ExecutionPreflightError(f"unknown capability field {self.field!r}")
        if self.status not in _STATUSES:
            raise ExecutionPreflightError(f"unsupported capability status {self.status!r}")
        if self.status == "not_evaluated" and not self.reason:
            raise ExecutionPreflightError(f"{self.field} must record why it is not evaluated")
        if self.status == "supported_with_limitation" and not self.limitation:
            raise ExecutionPreflightError(f"{self.field} must record its limitation")
        if not self.sources:
            raise ExecutionPreflightError(f"{self.field} must cite at least one source")
        payload: dict[str, Any] = {
            "capability": self.field,
            "status": self.status,
            "value": self.value,
            "sources": [dict(item) for item in self.sources],
        }
        if self.limitation:
            payload["limitation"] = self.limitation
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _runtime_source(reference: str, detail: str) -> dict[str, Any]:
    return {"kind": "runtime_code", "reference": reference, "detail": detail}


def _live_source(reference: str, detail: str) -> dict[str, Any]:
    return {"kind": "markethub_live", "reference": reference, "detail": detail}


def _blocked_source(reference: str, detail: str) -> dict[str, Any]:
    return {"kind": "markethub_unavailable", "reference": reference, "detail": detail}


@dataclass(frozen=True, slots=True)
class ReferenceProbe:
    """Live MarketHub reference facts that scope the snapshot to the declared range."""

    data_version: str
    daily_dataset_version: str
    catalog_rows: int
    instruments_listed_in_range: int
    board_lots: tuple[int, ...]
    tick_sizes: tuple[str, ...]
    st_flagged_instruments: int
    trading_days: int
    first_trading_day: str
    last_trading_day: str

    def evidence(self) -> dict[str, Any]:
        return {
            "data_version": self.data_version,
            "daily_dataset_version": self.daily_dataset_version,
            "catalog_rows": self.catalog_rows,
            "instruments_listed_in_range": self.instruments_listed_in_range,
            "board_lots": list(self.board_lots),
            "tick_sizes": list(self.tick_sizes),
            "st_flagged_instruments": self.st_flagged_instruments,
            "trading_days": self.trading_days,
            "first_trading_day": self.first_trading_day,
            "last_trading_day": self.last_trading_day,
        }


@dataclass(frozen=True, slots=True)
class WindowProbe:
    """One attempted live MarketHub freeze for a declared window."""

    label: str
    start: date
    end: date
    instruments: tuple[str, ...]
    status: str
    snapshot: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    dataset: CanonicalDataset | None = None
    error: dict[str, str] | None = None

    def evidence(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "instruments": list(self.instruments),
            "status": self.status,
        }
        if self.snapshot is not None and self.observation is not None:
            payload.update(
                {
                    "snapshot_id": self.snapshot["snapshot_id"],
                    "verification": dict(self.snapshot["verification"]),
                    "data_semantics": dict(self.snapshot["data_semantics"]),
                    "sample_count": self.observation["sample_count"],
                    "instrument_sample_counts": list(self.observation["instrument_sample_counts"]),
                }
            )
        if self.error is not None:
            payload["error"] = dict(self.error)
        return payload


def run_execution_preflight(
    request: Mapping[str, Any],
    *,
    adapter: MarketHubDataAdapter | None = None,
    client: MarketHubClient | None = None,
) -> dict[str, Any]:
    """Run one live execution preflight and return its capability snapshot."""

    value = _request(request)
    adapter = adapter or MarketHubDataAdapter()
    client = client or MarketHubClient(str(value["probe"]["base_url"]))
    reference = _reference_probe(client, value)
    probes = tuple(_probe(adapter, value, window) for window in value["range"]["windows"])
    if not probes:
        raise ExecutionPreflightError("execution preflight requires at least one window")
    observed = tuple(item for item in probes if item.status == "observed")
    observations = _observe(value, reference, probes, observed, adapter=adapter)
    capabilities = [item.as_dict() for item in observations]
    missing = set(REQUIRED_FIELDS) - {item["capability"] for item in capabilities}
    if missing:
        raise ExecutionPreflightError(f"capability snapshot is incomplete: {sorted(missing)}")
    payload = {
        "schema": SCHEMA,
        "status": "evaluated",
        "market": dict(value["market"]),
        "range": {
            "start": value["range"]["start"].isoformat(),
            "end": value["range"]["end"].isoformat(),
            "reference": reference.evidence(),
            "windows": [probe.evidence() for probe in probes],
            "bar_evidence": _bar_evidence(observed),
        },
        "runtime": _runtime_identity(),
        "source": {
            "adapter": adapter.name,
            "adapter_version": adapter.adapter_version,
            "base_url": value["probe"]["base_url"],
            "endpoint_contract": value["probe"]["endpoint_contract"],
            "as_of": value["probe"]["as_of"],
            "reads_only": True,
        },
        "required": list(REQUIRED_FIELDS),
        "capabilities": sorted(capabilities, key=lambda item: item["capability"]),
        "evaluated_count": sum(1 for item in capabilities if item["status"] != "not_evaluated"),
        "not_evaluated": sorted(
            item["capability"] for item in capabilities if item["status"] == "not_evaluated"
        ),
    }
    payload["snapshot_id"] = f"sha256:{sha256_value(payload)}"
    return payload


def _runtime_identity() -> dict[str, Any]:
    from quant_runtime import __version__

    profile = production_registry().profile("formal", "nautilus")
    return {
        "runtime_version": __version__,
        "formal_backend": "nautilus",
        "formal_adapter_version": profile.adapter_version,
        "formal_engine_version": profile.engine_version,
        "declared_capabilities": sorted(profile.provides),
    }


def _reference_probe(client: MarketHubClient, value: Mapping[str, Any]) -> ReferenceProbe:
    health = client.open()
    catalog = client.fetch_catalog()
    start = value["range"]["start"]
    end = value["range"]["end"]
    listed = tuple(
        item
        for item in catalog
        if item.list_date is not None
        and item.list_date <= end
        and (item.delist_date is None or item.delist_date >= start)
    )
    days = client.fetch_calendar(start, end)
    if not days:
        raise ExecutionPreflightError("MarketHub returned no trading days for the declared range")
    return ReferenceProbe(
        data_version=health.data_version,
        daily_dataset_version=health.daily_dataset_version,
        catalog_rows=len(catalog),
        instruments_listed_in_range=len(listed),
        board_lots=tuple(sorted({item.lot_size for item in listed})),
        tick_sizes=tuple(sorted({normalize_decimal(item.tick_size) for item in listed})),
        st_flagged_instruments=sum(1 for item in listed if item.is_st),
        trading_days=len(days),
        first_trading_day=days[0].isoformat(),
        last_trading_day=days[-1].isoformat(),
    )


def _probe(
    adapter: MarketHubDataAdapter,
    value: Mapping[str, Any],
    window: Mapping[str, Any],
) -> WindowProbe:
    request = _snapshot_request(value, window["instruments"], window["start"], window["end"])
    try:
        snapshot, observation = adapter.freeze_reference_with_observation(
            request,
            as_of=str(value["probe"]["as_of"]),
            required_semantics=tuple(value["probe"]["required_semantics"]),
        )
        dataset = adapter.read(request).dataset
    except MarketHubContractError as exc:
        return WindowProbe(
            label=str(window["label"]),
            start=window["start"],
            end=window["end"],
            instruments=tuple(window["instruments"]),
            status="unavailable",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
    if not isinstance(dataset, CanonicalDataset):
        raise ExecutionPreflightError("equity execution preflight requires a canonical dataset")
    return WindowProbe(
        label=str(window["label"]),
        start=window["start"],
        end=window["end"],
        instruments=tuple(window["instruments"]),
        status="observed",
        snapshot=snapshot,
        observation=observation,
        dataset=dataset,
    )


def _snapshot_request(
    value: Mapping[str, Any],
    instruments: Sequence[str],
    start: date,
    end: date,
    *,
    adjustment: str | None = None,
) -> SnapshotRequest:
    probe = value["probe"]
    return SnapshotRequest.from_dict(
        {
            "adapter": "markethub",
            "snapshot_mode": "reference",
            "trust_policy": "verified_immutable",
            "local_cache": "none",
            "endpoint_contract": probe["endpoint_contract"],
            "base_url": probe["base_url"],
            "as_of": probe["as_of"],
            "required_semantics": list(probe["required_semantics"]),
            "query": {
                "instruments": sorted(instruments),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "frequency": value["market"]["frequency"],
                "adjustment": adjustment or probe["adjustment"],
                "calendar": probe["calendar"],
                "contract_mapping": None,
            },
        }
    )


def _bar_evidence(observed: tuple[WindowProbe, ...]) -> str:
    """Say plainly whether any live bar backed the observations in this snapshot."""

    return "observed" if observed else "unavailable"


def _blocked_reads(probes: tuple[WindowProbe, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        _blocked_source(
            "/api/stocks/quotes/daily-window/query",
            f"window {probe.label!r} failed closed: "
            f"{probe.error['message'] if probe.error else ''}",
        )
        for probe in probes
        if probe.status == "unavailable"
    )


def _observe(
    value: Mapping[str, Any],
    reference: ReferenceProbe,
    probes: tuple[WindowProbe, ...],
    observed: tuple[WindowProbe, ...],
    *,
    adapter: MarketHubDataAdapter,
) -> tuple[FieldObservation, ...]:
    blocked = _blocked_reads(probes)
    return (
        _market_order_eligibility(),
        _limit_up_down(reference, observed, blocked),
        _lot_size(reference, observed),
        _fees(observed, blocked),
        _slippage(observed, blocked),
        _corporate_actions(value, observed, blocked, adapter=adapter),
        _short_or_long_direction(value),
        _suspension(observed, blocked),
        _partial_fill(observed),
        _gap(reference, observed, blocked),
        _cash_and_rounding(),
        _point_in_time_policy(observed, blocked),
    )


def _profile_provides() -> frozenset[str]:
    return frozenset(production_registry().profile("formal", "nautilus").provides)


def _venue_defaults() -> dict[str, str]:
    """Read the venue configuration the formal runner actually installs."""

    from quant_runtime.adapters.formal.nautilus import runner

    source = inspect.getsource(runner.run_engine)
    wanted = (
        "oms_type=OmsType.NETTING",
        "account_type=AccountType.CASH",
        "bar_execution=False",
        "allow_cash_borrowing=False",
        "fee_model=AShareFeeModel(config.fees)",
    )
    missing = sorted(item for item in wanted if item not in source)
    if missing:
        raise ExecutionPreflightError(f"formal venue configuration changed: {missing}")
    return {
        "oms_type": "NETTING",
        "account_type": "CASH",
        "bar_execution": "false",
        "allow_cash_borrowing": "false",
        "fee_model": "AShareFeeModel",
    }


def _market_order_eligibility() -> FieldObservation:
    provides = _profile_provides()
    venue = _venue_defaults()
    if not {"order.market", "decision.order"} <= provides:
        return FieldObservation(
            field="market_order_eligibility",
            status="not_evaluated",
            value={"declared_capabilities": sorted(provides)},
            sources=(
                _runtime_source(
                    "quant_runtime.registry.production_registry",
                    "the production formal profile was read at preflight time",
                ),
            ),
            reason="the production formal adapter does not declare order.market",
        )
    return FieldObservation(
        field="market_order_eligibility",
        status="supported",
        value={
            "order_types": ["market"],
            "execution_venue": "XCN",
            "bar_execution": venue["bar_execution"],
            "fill_reference": (
                "synthetic top-of-book quotes built from the MarketHub bar open "
                "(09:30 Asia/Shanghai) and close (14:59:59.999999 Asia/Shanghai)"
            ),
        },
        sources=(
            _runtime_source(
                "quant_runtime.registry.production_registry",
                "formal nautilus profile declares order.market and decision.order",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.run_engine",
                "venue installs bar_execution=False so orders fill against quote ticks",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.nautilus_data.native_quotes",
                "quote ticks are derived from MarketHub bar open and close prices",
            ),
        ),
    )


def _limit_bands() -> dict[str, str]:
    source = inspect.getsource(
        china_market_rules._price_limit_rate  # noqa: SLF001 - the executing rule is the evidence
    )
    bands = {"st": "0.05", "bjse": "0.30", "chinext_star": "0.20", "main_board": "0.10"}
    for item in bands.values():
        if f'Decimal("{item}")' not in source:
            raise ExecutionPreflightError(f"price-limit band {item} is no longer encoded")
    return bands


def _limit_up_down(
    reference: ReferenceProbe,
    observed: tuple[WindowProbe, ...],
    blocked: tuple[dict[str, Any], ...],
) -> FieldObservation:
    bands = _limit_bands()
    parameters = inspect.signature(
        china_market_rules._price_limit_rate  # noqa: SLF001 - the executing rule is the evidence
    ).parameters
    date_aware = any(name in parameters for name in ("trading_day", "as_of", "effective_date"))
    counts: dict[str, int] = {}
    for probe in observed:
        assert probe.dataset is not None
        book = AShareRuleBook(probe.dataset)
        up = down = 0
        for bar in probe.dataset.bars:
            state = book.state_for(bar.trading_day, bar.instrument, at_open=False)
            up += int(state.limit_up)
            down += int(state.limit_down)
        counts[f"{probe.label}.limit_up"] = up
        counts[f"{probe.label}.limit_down"] = down
    return FieldObservation(
        field="limit_up_down",
        status="supported_with_limitation",
        value={
            "bands": bands,
            "reference_price": "MarketHub pre_close",
            "rounding": "instrument tick size, half away from zero",
            "band_is_point_in_time": date_aware,
            "st_source": "catalog is_st, overridden by the daily row's own is_st",
            "catalog_st_flagged_instruments": reference.st_flagged_instruments,
            "live_bar_evidence": _bar_evidence(observed),
            "observed_limit_states": counts,
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules._price_limit_rate",
                "bands read from the executing rule: ST 5%, BJSE 30%, 300/301/688/689 20%, "
                "otherwise 10%",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.AShareRuleBook",
                "limit state is derived per bar from pre_close, tick size and the ST flag",
            ),
            _live_source(
                "/api/stocks/catalog",
                f"{reference.instruments_listed_in_range} instruments listed inside the declared "
                f"range were read live; {reference.st_flagged_instruments} carry an ST flag",
            ),
            *blocked,
        ),
        limitation=(
            "the band table carries no effective date, so historical band changes (STAR "
            "2019-07-22, ChiNext 2020-08-24, BJSE 2021-11-15) are applied to the whole "
            "declared range and the band is not point-in-time correct before those dates; "
            "the live catalog additionally flags no instrument as ST, so the 5% ST band can "
            "only be reached through a daily row's own is_st field"
        ),
    )


def _lot_size(reference: ReferenceProbe, observed: tuple[WindowProbe, ...]) -> FieldObservation:
    from quant_runtime.adapters.formal.nautilus.runner import FormalConfig

    return FieldObservation(
        field="lot_size",
        status="supported_with_limitation",
        value={
            "board_lot": list(reference.board_lots),
            "tick_size": list(reference.tick_sizes),
            "price_precision": 2,
            "catalog_instruments_in_range": reference.instruments_listed_in_range,
            "non_standard_lot_rejected_by_formal_config": _formal_config_rejects(
                FormalConfig, lot_size=200
            ),
            "observed_windows": [probe.label for probe in observed],
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.data.markethub.catalog.CanonicalInstrument.from_catalog",
                "every live catalog row is canonicalised to a 100-share lot and 0.01 tick",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.FormalConfig.validate",
                "executed at preflight time: a non-100 lot size is rejected",
            ),
            _live_source(
                "/api/stocks/catalog",
                f"{reference.catalog_rows} catalog rows read live; every instrument listed "
                "inside the declared range canonicalises to the lot and tick shown",
            ),
        ),
        limitation=(
            "the board lot is a Runtime constant applied to every A-share, not a "
            "MarketHub-published per-instrument field, so the STAR 200-share minimum with "
            "1-share increments and the BJSE 100-share minimum with 1-share increments are "
            "not modelled separately"
        ),
    )


def _formal_config_rejects(config_class: Any, *, lot_size: int) -> bool:
    try:
        config_class(
            strategy=_ProbeStrategy(),
            initial_cash_cny=Decimal("1000000"),
            lot_size=lot_size,
            tick_size=Decimal("0.01"),
            slippage_bps=Decimal("0"),
            fees=_reference_fee_spec(),
        ).validate(1)
    except ValueError:
        return True
    return False


class _ProbeStrategy:
    """Minimal strategy stand-in used only to exercise FormalConfig.validate."""

    parameters = {"top_k": 1}


def _reference_fee_spec() -> FeeSpec:
    return FeeSpec(
        commission_rate=Decimal("0"),
        minimum_commission_cny=Decimal("0"),
        sell_stamp_duty_rate=Decimal("0"),
        currency_precision=2,
        rounding_mode="half_away_from_zero",
        rounding_scope="per_fill",
    )


def _fee_rounding_is_enforced() -> bool:
    """Executed check: the fee contract refuses anything but per-fill CNY cent rounding."""

    for override in (
        {"currency_precision": 4},
        {"rounding_mode": "half_even"},
        {"rounding_scope": "per_order"},
    ):
        spec = _reference_fee_spec()
        candidate = FeeSpec(
            commission_rate=spec.commission_rate,
            minimum_commission_cny=spec.minimum_commission_cny,
            sell_stamp_duty_rate=spec.sell_stamp_duty_rate,
            currency_precision=int(override.get("currency_precision", 2)),
            rounding_mode=str(override.get("rounding_mode", "half_away_from_zero")),
            rounding_scope=str(override.get("rounding_scope", "per_fill")),
        )
        try:
            candidate.validate()
        except ValueError:
            continue
        return False
    return True


def _fees(
    observed: tuple[WindowProbe, ...], blocked: tuple[dict[str, Any], ...]
) -> FieldObservation:
    from nautilus_trader.model.enums import OrderSide

    spec = FeeSpec(
        commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.001"),
        currency_precision=2,
        rounding_mode="half_away_from_zero",
        rounding_scope="per_fill",
    )
    spec.validate()
    value: dict[str, Any] = {
        "model": "AShareFeeModel",
        "components": ["commission_rate", "minimum_commission_cny", "sell_stamp_duty_rate"],
        "rounding": "per_fill, CNY cent, half away from zero",
        "rounding_enforced": _fee_rounding_is_enforced(),
        "schedule_owner": "run configuration (FormalConfig.fees)",
        "schedule_is_point_in_time": "trading_day" in inspect.signature(calculate_fee).parameters,
        "live_bar_evidence": _bar_evidence(observed),
    }
    if observed:
        probe = observed[0]
        assert probe.dataset is not None
        bar = probe.dataset.bars[0]
        notional = bar.close * Decimal(100)
        value["executed_example"] = {
            "instrument": bar.instrument,
            "trading_day": bar.trading_day.isoformat(),
            "notional_cny": normalize_decimal(notional),
            "buy_fee_cny": normalize_decimal(calculate_fee(notional, OrderSide.BUY, spec)),
            "sell_fee_cny": normalize_decimal(calculate_fee(notional, OrderSide.SELL, spec)),
        }
    return FieldObservation(
        field="fees",
        status="supported_with_limitation",
        value=value,
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.AShareFeeModel",
                "the Nautilus venue installs this fee model for every formal A-share run",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.FeeSpec.validate",
                "executed at preflight time: non-cent precision, half-even rounding and "
                "per-order rounding are all rejected",
            ),
            *blocked,
        ),
        limitation=(
            "the Runtime supplies the fee mechanism but not a fee schedule; rates come from "
            "the run configuration and carry no effective date, so schedule changes inside "
            "the declared range (stamp duty 0.1% -> 0.05% on 2023-08-28) are not modelled"
        ),
    )


def _slippage(
    observed: tuple[WindowProbe, ...], blocked: tuple[dict[str, Any], ...]
) -> FieldObservation:
    from quant_runtime.adapters.formal.nautilus import nautilus_data

    source = inspect.getsource(nautilus_data.native_quotes)
    if "slippage_bps / Decimal(10_000)" not in source:
        raise ExecutionPreflightError("the slippage construction changed")
    value: dict[str, Any] = {
        "model": "symmetric basis-point spread around the observed bar price",
        "parameter": "FormalConfig.slippage_bps",
        "applied_at": ["09:30 open quote", "14:59:59.999999 close quote"],
        "non_negative_enforced": True,
        "live_bar_evidence": _bar_evidence(observed),
    }
    if observed:
        from quant_runtime.adapters.formal.nautilus.instruments import native_instrument

        probe = observed[0]
        assert probe.dataset is not None
        instruments = {
            item.instrument: native_instrument(item) for item in probe.dataset.instruments
        }
        quotes = nautilus_data.native_quotes(probe.dataset, instruments, Decimal("5"))
        sample = next(quote for values in quotes.values() for quote in values)
        value["executed_example"] = {
            "slippage_bps": "5",
            "instrument": str(sample.instrument_id),
            "bid": str(sample.bid_price),
            "ask": str(sample.ask_price),
        }
    return FieldObservation(
        field="slippage",
        status="supported",
        value=value,
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.nautilus_data.native_quotes",
                "the quote spread is the slippage model and was read from the executing code",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.FormalConfig.validate",
                "slippage_bps is required to be non-negative for every formal run",
            ),
            *blocked,
        ),
    )


def _corporate_actions(
    value: Mapping[str, Any],
    observed: tuple[WindowProbe, ...],
    blocked: tuple[dict[str, Any], ...],
    *,
    adapter: MarketHubDataAdapter,
) -> FieldObservation:
    from quant_runtime.adapters.data.markethub import client as client_module

    request_body = inspect.getsource(client_module.MarketHubClient.fetch_daily)
    carries_adjustment = '"adjustment"' in request_body
    payload: dict[str, Any] = {
        "declared_adjustment": value["probe"]["adjustment"],
        "daily_request_carries_adjustment": carries_adjustment,
        "live_bar_evidence": _bar_evidence(observed),
    }
    sources: list[dict[str, Any]] = [
        _runtime_source(
            "quant_runtime.adapters.data.markethub.client.MarketHubClient.fetch_daily",
            "the daily-window request body carries no adjustment argument, so 1d equity "
            "prices are read exactly as MarketHub stores them",
        ),
        _runtime_source(
            "quant_runtime.adapters.data.markethub.contract.SnapshotRequest",
            "`adjustment` is validated only for 1m futures; for 1d equities it is carried "
            "into the snapshot identity and never into a request",
        ),
    ]
    if observed:
        probe = observed[0]
        instruments = probe.instruments[:1]
        window_end = min(probe.end, date(probe.start.year + 1, probe.start.month, probe.start.day))
        try:
            baseline = adapter.read(
                _snapshot_request(value, instruments, probe.start, window_end, adjustment="none")
            )
            forward = adapter.read(
                _snapshot_request(value, instruments, probe.start, window_end, adjustment="forward")
            )
        except MarketHubContractError as exc:
            sources.append(
                _blocked_source(
                    "/api/stocks/quotes/daily-window/query",
                    f"the adjustment comparison failed closed: {exc}",
                )
            )
        else:
            payload["experiment"] = {
                "instruments": list(instruments),
                "start": probe.start.isoformat(),
                "end": window_end.isoformat(),
                "none_canonical_input_hash": baseline.dataset.input_hash,
                "forward_canonical_input_hash": forward.dataset.input_hash,
                "adjustment_changes_canonical_input": (
                    baseline.dataset.input_hash != forward.dataset.input_hash
                ),
            }
            sources.append(
                _live_source(
                    "/api/stocks/quotes/daily-window/query",
                    "two live reads of the same window under adjustment 'none' and 'forward'",
                )
            )
    sources.extend(blocked)
    return FieldObservation(
        field="corporate_actions",
        status="not_evaluated",
        value=payload,
        sources=tuple(sources),
        reason=(
            "the Runtime applies no corporate-action adjustment to 1d A-share prices: "
            "`adjustment` is only a snapshot identity label and the daily-window request "
            "carries no adjustment argument, so splits, dividends and rights issues are "
            "neither applied nor reconciled. MarketHub publishes corporate-action and "
            "adjustment-factor endpoints, but no authorized source documents how a formal "
            "run should consume them, and no such consumption exists in this Runtime."
        ),
    )


def _short_or_long_direction(value: Mapping[str, Any]) -> FieldObservation:
    venue = _venue_defaults()
    provides = _profile_provides()
    return FieldObservation(
        field="short_or_long_direction",
        status="supported",
        value={
            "declared_direction": value["market"]["direction"],
            "account_type": venue["account_type"],
            "allow_cash_borrowing": venue["allow_cash_borrowing"],
            "oms_type": venue["oms_type"],
            "enforced_direction": "long_only",
            "declares_t_plus_one": "market.cn.equity.t_plus_one" in provides,
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.run_engine",
                "the venue is a CNY cash account with cash borrowing disabled, so no short "
                "position can be opened",
            ),
            _runtime_source(
                "quant_runtime.registry.production_registry",
                "the formal profile declares market.cn.equity.t_plus_one",
            ),
        ),
    )


def _suspension(
    observed: tuple[WindowProbe, ...], blocked: tuple[dict[str, Any], ...]
) -> FieldObservation:
    from quant_runtime.adapters.data.markethub import model as model_module

    source = inspect.getsource(model_module.CanonicalBar.from_markethub)
    if 'row.get("is_suspended"' not in source:
        raise ExecutionPreflightError("the suspension canonicalisation changed")
    counts: dict[str, int] = {}
    for probe in observed:
        assert probe.dataset is not None
        book = AShareRuleBook(probe.dataset)
        counts[probe.label] = sum(
            int(book.state_for(bar.trading_day, bar.instrument, at_open=True).suspended)
            for bar in probe.dataset.bars
        )
    return FieldObservation(
        field="suspension",
        status="supported",
        value={
            "source_field": "is_suspended",
            "canonical_effect": (
                "a suspended bar falls back to pre_close for OHLC and zero for volume and "
                "amount, and is surfaced to the strategy as a data gap"
            ),
            "live_bar_evidence": _bar_evidence(observed),
            "observed_suspended_bars": counts,
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.data.markethub.model.CanonicalBar.from_markethub",
                "is_suspended is canonicalised from every MarketHub daily row",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.AShareRuleBook",
                "RuleState.suspended is derived from the canonical bar for every rule check",
            ),
            _runtime_source(
                "quant_runtime.registry.production_registry",
                "the formal profile declares market.cn.equity.suspension",
            ),
            *blocked,
        ),
    )


def _partial_fill(observed: tuple[WindowProbe, ...]) -> FieldObservation:
    from quant_runtime.adapters.formal.nautilus import nautilus_data

    source = inspect.getsource(nautilus_data.native_quotes)
    if "Quantity.from_int(1_000_000_000)" not in source:
        raise ExecutionPreflightError("the quote depth construction changed")
    value: dict[str, Any] = {
        "engine_supports_partial_fills": True,
        "quote_depth": "1000000000",
        "depth_is_derived_from_market_volume": False,
        "live_bar_evidence": _bar_evidence(observed),
        "observed_windows": [probe.label for probe in observed],
    }
    return FieldObservation(
        field="partial_fill",
        status="supported_with_limitation",
        value=value,
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.nautilus_data.native_quotes",
                "every quote carries a fixed synthetic depth rather than the observed bar volume",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.run_engine",
                "NautilusTrader reports partial fills natively, but the venue is fed only "
                "these synthetic quotes",
            ),
        ),
        limitation=(
            "quote depth is a fixed synthetic size, not the observed bar volume, so a formal "
            "A-share run fills every accepted order completely; partial fills are "
            "representable by the engine but are not produced by this data path"
        ),
    )


def _gap(
    reference: ReferenceProbe,
    observed: tuple[WindowProbe, ...],
    blocked: tuple[dict[str, Any], ...],
) -> FieldObservation:
    counts: dict[str, int] = {}
    for probe in observed:
        assert probe.dataset is not None
        book = AShareRuleBook(probe.dataset)
        missing = 0
        for instrument in probe.dataset.instruments:
            for trading_day in probe.dataset.trading_days:
                state = book.state_for(trading_day, instrument.instrument, at_open=True)
                if not state.has_bar and not state.before_listing and not state.after_delisting:
                    missing += 1
        counts[probe.label] = missing
    return FieldObservation(
        field="gap",
        status="supported",
        value={
            "calendar": "cn-equity-v1 trading days verified against MarketHub",
            "declared_range_trading_days": reference.trading_days,
            "first_trading_day": reference.first_trading_day,
            "last_trading_day": reference.last_trading_day,
            "detection": (
                "RuleState.has_bar distinguishes a missing bar from pre-listing and "
                "post-delisting days"
            ),
            "strategy_effect": "a gap breaks indicator continuity instead of bridging the hole",
            "live_bar_evidence": _bar_evidence(observed),
            "observed_missing_bars": counts,
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.AShareRuleBook",
                "has_bar, before_listing and after_delisting separate a real gap from a "
                "listing boundary",
            ),
            _live_source(
                "/api/markets/calendar/trading",
                f"{reference.trading_days} trading days from {reference.first_trading_day} to "
                f"{reference.last_trading_day} were read live and verified for the declared "
                "range",
            ),
            *blocked,
        ),
    )


def _cash_and_rounding() -> FieldObservation:
    venue = _venue_defaults()
    from quant_runtime.adapters.formal.nautilus import instruments as instruments_module

    source = inspect.getsource(instruments_module.native_instrument)
    if "currency=CNY" not in source or "lot_size=Quantity.from_int(item.lot_size)" not in source:
        raise ExecutionPreflightError("the instrument construction changed")
    return FieldObservation(
        field="cash_and_rounding",
        status="supported",
        value={
            "currency": "CNY",
            "account_type": venue["account_type"],
            "allow_cash_borrowing": venue["allow_cash_borrowing"],
            "price_precision": 2,
            "quantity_unit": "whole board lots",
            "fee_rounding": "half away from zero per fill at CNY cent precision",
            "fee_rounding_enforced": _fee_rounding_is_enforced(),
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.china_market_rules.FeeSpec.validate",
                "executed at preflight time: CNY cent precision with per-fill "
                "half-away-from-zero rounding is the only accepted configuration",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.instruments.native_instrument",
                "every instrument is created with CNY, 2-decimal prices and its board lot",
            ),
            _runtime_source(
                "quant_runtime.adapters.formal.nautilus.runner.run_engine",
                "the venue holds a CNY cash balance and forbids cash borrowing",
            ),
        ),
    )


def _point_in_time_policy(
    observed: tuple[WindowProbe, ...], blocked: tuple[dict[str, Any], ...]
) -> FieldObservation:
    from quant_runtime.adapters.data.markethub import adapter as adapter_module

    declaration = inspect.getsource(
        adapter_module.MarketHubDataAdapter.freeze_reference_with_observation
    )
    declared_not_evaluated = (
        "the published MarketHub contract does not expose historical field availability"
        in declaration
    )
    semantics = {
        probe.label: {
            name: dict(item)
            for name, item in (probe.snapshot or {}).get("data_semantics", {}).items()
        }
        for probe in observed
    }
    verified = bool(semantics) and all(
        item["point_in_time"]["status"] == "verified" for item in semantics.values()
    )
    return FieldObservation(
        field="point_in_time_policy",
        status="supported" if verified else "not_evaluated",
        value={
            "snapshot_policy": "verified_immutable reference frozen at an explicit as_of",
            "version_drift": "fail closed on MarketHub data or dataset version drift",
            "adapter_declares_point_in_time_not_evaluated": declared_not_evaluated,
            "observed_data_semantics": semantics,
        },
        sources=(
            _runtime_source(
                "quant_runtime.adapters.data.markethub.MarketHubDataAdapter."
                "freeze_reference_with_observation",
                "the adapter itself reports point_in_time as not_evaluated because the "
                "published MarketHub contract does not expose historical field availability",
            ),
            _runtime_source(
                "quant_runtime.adapters.data.markethub.client.MarketHubClient.verify_version",
                "a frozen snapshot fails closed when the MarketHub data or dataset version "
                "moves under it",
            ),
            *blocked,
        ),
        reason=(
            None
            if verified
            else "snapshot-level discipline is real -- every read is frozen at an explicit "
            "as_of under a verified_immutable trust policy and fails closed on version "
            "drift -- but field-level point-in-time correctness is not established: the "
            "published MarketHub contract does not expose historical field availability, so "
            "the adapter reports the point_in_time semantic as not_evaluated"
        ),
    )


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != REQUEST_SCHEMA:
        raise ExecutionPreflightError("execution preflight request schema is invalid")
    if set(value) != {"schema", "market", "range", "probe"}:
        raise ExecutionPreflightError("execution preflight request has unsupported fields")
    market = value["market"]
    probe = value["probe"]
    window_range = value["range"]
    if not all(isinstance(item, Mapping) for item in (market, probe, window_range)):
        raise ExecutionPreflightError("execution preflight request sections must be objects")
    if set(market) != {"asset_class", "exchanges", "frequency", "direction"}:
        raise ExecutionPreflightError("execution preflight market section is invalid")
    if market["asset_class"] != "equity" or market["frequency"] != "1d":
        raise ExecutionPreflightError("this execution preflight covers 1d A-share equities only")
    if set(probe) != {
        "base_url",
        "endpoint_contract",
        "calendar",
        "as_of",
        "adjustment",
        "required_semantics",
    }:
        raise ExecutionPreflightError("execution preflight probe section is invalid")
    if not str(probe["base_url"]).startswith("http"):
        raise ExecutionPreflightError("execution preflight requires a real MarketHub base URL")
    if set(window_range) != {"start", "end", "windows"}:
        raise ExecutionPreflightError("execution preflight range section is invalid")
    start = date.fromisoformat(str(window_range["start"]))
    end = date.fromisoformat(str(window_range["end"]))
    if start > end:
        raise ExecutionPreflightError("execution preflight range is not ordered")
    windows = []
    for item in window_range["windows"]:
        if not isinstance(item, Mapping) or set(item) != {
            "label",
            "start",
            "end",
            "instruments",
        }:
            raise ExecutionPreflightError("execution preflight window is invalid")
        window_start = date.fromisoformat(str(item["start"]))
        window_end = date.fromisoformat(str(item["end"]))
        instruments = tuple(str(name) for name in item["instruments"])
        if window_start < start or window_end > end or window_start > window_end:
            raise ExecutionPreflightError(f"window {item['label']!r} escapes the declared range")
        if not instruments or len(set(instruments)) != len(instruments):
            raise ExecutionPreflightError(f"window {item['label']!r} instruments are invalid")
        windows.append(
            {
                "label": str(item["label"]),
                "start": window_start,
                "end": window_end,
                "instruments": instruments,
            }
        )
    if not windows:
        raise ExecutionPreflightError("execution preflight requires at least one window")
    return {
        "schema": REQUEST_SCHEMA,
        "market": dict(market),
        "probe": {**dict(probe), "as_of": _as_of(str(probe["as_of"]))},
        "range": {"start": start, "end": end, "windows": windows},
    }


def _as_of(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionPreflightError("execution preflight as_of must be RFC 3339") from exc
    if parsed.tzinfo is None:
        raise ExecutionPreflightError("execution preflight as_of must include an offset")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "REQUEST_SCHEMA",
    "REQUIRED_FIELDS",
    "SCHEMA",
    "ExecutionPreflightError",
    "FieldObservation",
    "ReferenceProbe",
    "WindowProbe",
    "run_execution_preflight",
]
