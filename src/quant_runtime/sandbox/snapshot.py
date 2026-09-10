from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_runtime.adapters.data.markethub import ResolvedSnapshot
from quant_runtime.adapters.data.markethub.catalog import CanonicalInstrument
from quant_runtime.adapters.data.markethub.futures_model import (
    CanonicalFuturesBar,
    CanonicalFuturesDataset,
    CanonicalFuturesInstrument,
    FuturesContractCatalogIdentity,
    ReplayablePartialFuturesBars,
)
from quant_runtime.adapters.data.markethub.model import CanonicalBar, CanonicalDataset
from quant_runtime.artifacts import canonical_json, sha256_value

SNAPSHOT_CAPSULE_SCHEMA = "quant-runtime.sandbox-snapshot-capsule.v1"


def build_snapshot_capsule(snapshot: ResolvedSnapshot) -> dict[str, Any]:
    dataset = snapshot.dataset
    if not isinstance(dataset, CanonicalDataset | CanonicalFuturesDataset):
        raise ValueError("sandbox formal execution requires a verified canonical snapshot")
    common = {
        "schema": SNAPSHOT_CAPSULE_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_mode": snapshot.mode,
        "canonical_input_hash": dataset.input_hash,
        "data_version": dataset.data_version,
        "dataset_version": dataset.dataset_version,
        "timezone": dataset.timezone,
    }
    if isinstance(dataset, CanonicalDataset):
        identity = {
            **common,
            "kind": "equity-1d",
            "instruments": [item.hash_record() for item in dataset.instruments],
            "trading_days": [item.isoformat() for item in dataset.trading_days],
            "bars": [item.hash_record() for item in dataset.bars],
        }
    elif isinstance(dataset, CanonicalFuturesDataset):
        partial = (
            dataset.bars.verification
            if isinstance(dataset.bars, ReplayablePartialFuturesBars)
            else None
        )
        identity = {
            **common,
            "kind": "futures-1m",
            "series_type": dataset.series_type,
            "instruments": [item.hash_record() for item in dataset.instruments],
            "bars": [item.hash_record() for item in dataset.bars],
            "contract_catalog": (
                dataset.contract_catalog.hash_record() if dataset.contract_catalog else None
            ),
            "partial_lineage": dataset.partial_lineage,
            "partial_verification": partial,
        }
    else:
        raise ValueError("sandbox formal execution requires a verified canonical snapshot")
    return {**identity, "capsule_id": "sha256:" + sha256_value(identity)}


def snapshot_capsule_bytes(value: dict[str, Any]) -> bytes:
    return canonical_json(value)


def load_snapshot_capsule(path: Path, *, snapshot_id: str) -> ResolvedSnapshot:
    try:
        supplied = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sandbox snapshot capsule cannot be read") from exc
    if not isinstance(supplied, dict) or supplied.get("schema") != SNAPSHOT_CAPSULE_SCHEMA:
        raise ValueError("sandbox snapshot capsule schema is invalid")
    capsule_id = supplied.pop("capsule_id", None)
    if capsule_id != "sha256:" + sha256_value(supplied):
        raise ValueError("sandbox snapshot capsule identity is invalid")
    if supplied.get("snapshot_id") != snapshot_id:
        raise ValueError("sandbox snapshot capsule identity differs")
    try:
        if supplied.get("kind") == "equity-1d":
            dataset = CanonicalDataset(
                data_version=str(supplied["data_version"]),
                dataset_version=str(supplied["dataset_version"]),
                timezone=str(supplied["timezone"]),
                instruments=tuple(_instrument(item) for item in supplied["instruments"]),
                trading_days=tuple(
                    date.fromisoformat(str(item)) for item in supplied["trading_days"]
                ),
                bars=tuple(_bar(item) for item in supplied["bars"]),
            )
        elif supplied.get("kind") == "futures-1m":
            dataset = _futures_dataset(supplied)
        else:
            raise ValueError("sandbox snapshot kind is invalid")
        if dataset.input_hash != supplied["canonical_input_hash"]:
            raise ValueError("sandbox snapshot canonical input identity differs")
        mode = str(supplied["snapshot_mode"])
        if mode not in {"reference", "materialized"}:
            raise ValueError("sandbox snapshot mode is invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("sandbox snapshot capsule content is invalid") from exc
    return ResolvedSnapshot(
        {
            "snapshot_id": snapshot_id,
            "mode": mode,
            "source": "sealed-sandbox-capsule",
        },
        Path("/sandbox/inputs/snapshot-capsule.json"),
        dataset,
    )


def _instrument(value: Any) -> CanonicalInstrument:
    if not isinstance(value, dict):
        raise ValueError("sandbox snapshot instrument is invalid")
    return CanonicalInstrument(
        instrument=str(value["instrument"]),
        raw_code=str(value["raw_code"]),
        exchange=str(value["exchange"]),
        currency=str(value["currency"]),
        price_precision=int(value["price_precision"]),
        tick_size=Decimal(str(value["tick_size"])),
        lot_size=int(value["lot_size"]),
        list_date=date.fromisoformat(value["list_date"]) if value.get("list_date") else None,
        delist_date=date.fromisoformat(value["delist_date"]) if value.get("delist_date") else None,
        is_st=bool(value["is_st"]),
    )


def _bar(value: Any) -> CanonicalBar:
    if not isinstance(value, dict):
        raise ValueError("sandbox snapshot bar is invalid")
    return CanonicalBar(
        trading_day=date.fromisoformat(str(value["trading_day"])),
        instrument=str(value["instrument"]),
        open=Decimal(str(value["open"])),
        high=Decimal(str(value["high"])),
        low=Decimal(str(value["low"])),
        close=Decimal(str(value["close"])),
        volume=Decimal(str(value["volume"])),
        amount=Decimal(str(value["amount"])),
        pre_close=Decimal(str(value["pre_close"])),
        is_suspended=bool(value["is_suspended"]),
        is_st=bool(value["is_st"]),
    )


def _futures_dataset(value: dict[str, Any]) -> CanonicalFuturesDataset:
    frozen_bars = tuple(_futures_bar(item) for item in value["bars"])
    bars: tuple[CanonicalFuturesBar, ...] | ReplayablePartialFuturesBars = frozen_bars
    verification = value.get("partial_verification")
    if verification is not None:
        if not isinstance(verification, dict):
            raise ValueError("sandbox partial verification is invalid")
        instruments = tuple(str(item["instrument"]) for item in value["instruments"])
        bars = ReplayablePartialFuturesBars(
            instruments=instruments,
            bar_counts={
                instrument: sum(item.instrument == instrument for item in frozen_bars)
                for instrument in instruments
            },
            trading_dates=tuple(sorted({item.bar_time.date() for item in frozen_bars})),
            instrument_bounds={
                instrument: (
                    min(item.bar_time for item in frozen_bars if item.instrument == instrument),
                    max(item.bar_time for item in frozen_bars if item.instrument == instrument),
                )
                for instrument in instruments
            },
            verified_input_hash=str(value["canonical_input_hash"]),
            verification=verification,
            _stream_factory=lambda: iter(frozen_bars),
        )
    catalog = value.get("contract_catalog")
    return CanonicalFuturesDataset(
        data_version=str(value["data_version"]),
        dataset_version=str(value["dataset_version"]),
        timezone=str(value["timezone"]),
        series_type=str(value["series_type"]),
        instruments=tuple(_futures_instrument(item) for item in value["instruments"]),
        bars=bars,
        contract_catalog=(
            FuturesContractCatalogIdentity(
                schema_version=str(catalog["schema_version"]),
                dataset_version=str(catalog["dataset_version"]),
                snapshot_id=str(catalog["snapshot_id"]),
                content_checksum=str(catalog["content_checksum"]),
            )
            if isinstance(catalog, dict)
            else None
        ),
        partial_lineage=value.get("partial_lineage"),
    )


def _futures_instrument(value: Any) -> CanonicalFuturesInstrument:
    if not isinstance(value, dict):
        raise ValueError("sandbox futures instrument is invalid")
    return CanonicalFuturesInstrument(
        instrument=str(value["instrument"]),
        product_code=str(value["product_code"]),
        exchange=str(value["exchange"]),
        series_type=str(value["series_type"]),
        price_precision=int(value["price_precision"]) if "price_precision" in value else None,
        tick_size=Decimal(str(value["tick_size"])) if "tick_size" in value else None,
        multiplier=Decimal(str(value["multiplier"])) if "multiplier" in value else None,
        currency=str(value["currency"]) if "currency" in value else None,
    )


def _futures_bar(value: Any) -> CanonicalFuturesBar:
    if not isinstance(value, dict):
        raise ValueError("sandbox futures bar is invalid")
    return CanonicalFuturesBar(
        bar_time=datetime.fromisoformat(str(value["bar_time"])),
        instrument=str(value["instrument"]),
        signal_open=Decimal(str(value["signal_open"])),
        signal_high=Decimal(str(value["signal_high"])),
        signal_low=Decimal(str(value["signal_low"])),
        signal_close=Decimal(str(value["signal_close"])),
        volume=Decimal(str(value["volume"])),
        open_interest=(
            Decimal(str(value["open_interest"])) if value.get("open_interest") is not None else None
        ),
        adjustment_offset=Decimal(str(value["adjustment_offset"])),
    )
