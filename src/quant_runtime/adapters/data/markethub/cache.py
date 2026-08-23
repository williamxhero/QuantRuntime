from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from quant_runtime.contracts.canonical_hash import (
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_value,
    write_json,
)
from quant_runtime.market_data.markethub.catalog import CanonicalInstrument
from quant_runtime.market_data.markethub.client import MarketHubContractError
from quant_runtime.market_data.markethub.daily_data import CanonicalBar, CanonicalDataset
from quant_runtime.workspace.atomic import AtomicDirectory
from quant_runtime.workspace.layout import RuntimeLayout

CACHE_TRANSFORM_VERSION = "markethub-canonical-daily-cache-v1"


@dataclass(frozen=True, slots=True)
class CacheUse:
    policy: str
    path: Path | None
    transform_version: str | None
    evidence_manifest: Path


class MarketHubCache:
    """A verified, non-authoritative conversion cache for formal consumers."""

    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout

    @contextmanager
    def prepare(
        self,
        *,
        policy: str,
        snapshot_id: str,
        dataset: CanonicalDataset,
        consumer: str,
        run_id: str,
        evidence_root: Path,
    ) -> Iterator[CacheUse]:
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / "cache_conversion_manifest.json"
        base = {
            "schema": "quant-research.cache-conversion.v1",
            "policy": policy,
            "authoritative": False,
            "source_snapshot_id": snapshot_id,
            "source_input_hash": dataset.input_hash,
            "consumer": consumer,
        }
        if policy == "none":
            write_json(
                evidence_path,
                {**base, "transform_version": None, "output": None, "retained": False},
            )
            yield CacheUse(policy, None, None, evidence_path)
            return

        payloads = _cache_payloads(dataset)
        files = {
            name: {"sha256": sha256_bytes(payload), "content_bytes": len(payload)}
            for name, payload in payloads.items()
        }
        content_hash = sha256_value({"transform_version": CACHE_TRANSFORM_VERSION, "files": files})
        output = {
            "sha256": content_hash,
            "format": "canonical-dataset-directory",
            "files": files,
        }
        if policy == "persistent":
            identity = sha256_value(
                {
                    "snapshot_id": snapshot_id,
                    "consumer": consumer,
                    "transform_version": CACHE_TRANSFORM_VERSION,
                    "content_hash": content_hash,
                }
            )
            final = (
                self._layout.cache
                / "persistent"
                / snapshot_id.removeprefix("sha256:")
                / consumer
                / identity
            )
            reused = final.exists()
            if reused:
                loaded = self.load(final)
                if loaded.input_hash != dataset.input_hash:
                    raise MarketHubContractError("persistent cache canonical input hash mismatch")
            else:
                with AtomicDirectory(self._layout.staging, final) as staging:
                    _write_payloads(staging.path, payloads)
                    write_json(
                        staging.path / "cache_manifest.json",
                        {
                            **base,
                            "transform_version": CACHE_TRANSFORM_VERSION,
                            "output": output,
                            "retained": True,
                        },
                    )
                    staging.publish()
            write_json(
                evidence_path,
                {
                    **base,
                    "transform_version": CACHE_TRANSFORM_VERSION,
                    "output": output,
                    "retained": True,
                    "reused": reused,
                    "cache_path": str(final.resolve()),
                },
            )
            yield CacheUse(policy, final.resolve(), CACHE_TRANSFORM_VERSION, evidence_path)
            return

        if policy != "ephemeral":
            raise ValueError(f"unsupported cache policy {policy!r}")
        ephemeral_root = self._layout.staging / "cache" / run_id / f"{consumer}-{uuid4().hex}"
        ephemeral_root.mkdir(parents=True, exist_ok=False)
        _write_payloads(ephemeral_root, payloads)
        write_json(
            ephemeral_root / "cache_manifest.json",
            {
                **base,
                "transform_version": CACHE_TRANSFORM_VERSION,
                "output": output,
                "retained": False,
            },
        )
        write_json(
            evidence_path,
            {
                **base,
                "transform_version": CACHE_TRANSFORM_VERSION,
                "output": output,
                "retained": False,
                "cache_path": None,
            },
        )
        try:
            yield CacheUse(
                policy,
                ephemeral_root.resolve(),
                CACHE_TRANSFORM_VERSION,
                evidence_path,
            )
        finally:
            resolved = ephemeral_root.resolve()
            expected = (self._layout.staging / "cache" / run_id).resolve()
            if expected not in resolved.parents:
                raise RuntimeError(f"ephemeral cache escaped expected root: {resolved}")
            shutil.rmtree(resolved)

    @staticmethod
    def load(root: Path) -> CanonicalDataset:
        """Verify and reconstruct the exact canonical dataset stored in a cache."""

        manifest = read_json(root / "cache_manifest.json")
        if manifest.get("authoritative") is not False:
            raise MarketHubContractError("cache must be marked non-authoritative")
        if manifest.get("transform_version") != CACHE_TRANSFORM_VERSION:
            raise MarketHubContractError("cache transform version mismatch")
        output = manifest.get("output")
        files = output.get("files") if isinstance(output, dict) else None
        if not isinstance(files, dict) or set(files) != {"bars.parquet", "dataset.json"}:
            raise MarketHubContractError("cache manifest has an invalid file set")
        for name, record in files.items():
            if not isinstance(record, dict):
                raise MarketHubContractError(f"cache file record is invalid: {name}")
            payload = (root / name).read_bytes()
            if len(payload) != int(record.get("content_bytes", -1)):
                raise MarketHubContractError(f"cache byte count mismatch: {name}")
            if sha256_bytes(payload) != record.get("sha256"):
                raise MarketHubContractError(f"cache sha256 mismatch: {name}")
        metadata = read_json(root / "dataset.json")
        try:
            instruments = tuple(_instrument(item) for item in metadata["instruments"])
            trading_days = tuple(date.fromisoformat(item) for item in metadata["trading_days"])
            rows = pq.read_table(root / "bars.parquet").to_pylist()
            bars = tuple(_bar(item) for item in rows)
            dataset = CanonicalDataset(
                data_version=str(metadata["data_version"]),
                dataset_version=str(metadata["dataset_version"]),
                timezone=str(metadata["timezone"]),
                instruments=instruments,
                trading_days=trading_days,
                bars=bars,
            )
            dataset.validate()
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketHubContractError(f"cache canonical dataset is invalid: {exc}") from exc
        if dataset.input_hash != metadata.get("canonical_input_hash"):
            raise MarketHubContractError("cache canonical input hash mismatch")
        return dataset


def _cache_payloads(dataset: CanonicalDataset) -> dict[str, bytes]:
    dataset.validate()
    table = pa.table(
        {
            "trading_day": [item.trading_day for item in dataset.bars],
            "instrument": [item.instrument for item in dataset.bars],
            "open": [str(item.open) for item in dataset.bars],
            "high": [str(item.high) for item in dataset.bars],
            "low": [str(item.low) for item in dataset.bars],
            "close": [str(item.close) for item in dataset.bars],
            "volume": [str(item.volume) for item in dataset.bars],
            "amount": [str(item.amount) for item in dataset.bars],
            "pre_close": [str(item.pre_close) for item in dataset.bars],
            "is_suspended": [item.is_suspended for item in dataset.bars],
            "is_st": [item.is_st for item in dataset.bars],
        }
    ).replace_schema_metadata({b"transform_version": CACHE_TRANSFORM_VERSION.encode()})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    metadata = {
        "schema": "quant-research.canonical-cache-dataset.v1",
        "transform_version": CACHE_TRANSFORM_VERSION,
        "canonical_input_hash": dataset.input_hash,
        "data_version": dataset.data_version,
        "dataset_version": dataset.dataset_version,
        "timezone": dataset.timezone,
        "instruments": [item.hash_record() for item in dataset.instruments],
        "trading_days": [item.isoformat() for item in dataset.trading_days],
    }
    return {
        "bars.parquet": sink.getvalue().to_pybytes(),
        "dataset.json": canonical_json(metadata),
    }


def _write_payloads(root: Path, payloads: dict[str, bytes]) -> None:
    for name, payload in payloads.items():
        (root / name).write_bytes(payload)


def _instrument(value: dict[str, Any]) -> CanonicalInstrument:
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
        is_st=bool(value.get("is_st", False)),
    )


def _bar(value: dict[str, Any]) -> CanonicalBar:
    return CanonicalBar(
        trading_day=value["trading_day"],
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
