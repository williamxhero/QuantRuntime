from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from quant_runtime.contracts.canonical_hash import sha256_bytes, sha256_value, write_json
from quant_runtime.market_data.markethub.client import MarketHubContractError
from quant_runtime.market_data.markethub.daily_data import CanonicalDataset
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
            "consumer": consumer,
        }
        if policy == "none":
            write_json(
                evidence_path,
                {**base, "transform_version": None, "output": None, "retained": False},
            )
            yield CacheUse(policy, None, None, evidence_path)
            return
        payload = _cache_bytes(dataset)
        digest = sha256_bytes(payload)
        output = {"sha256": digest, "content_bytes": len(payload), "format": "parquet"}
        if policy == "persistent":
            identity = sha256_value(
                {
                    "snapshot_id": snapshot_id,
                    "consumer": consumer,
                    "transform_version": CACHE_TRANSFORM_VERSION,
                    "output_sha256": digest,
                }
            )
            final = (
                self._layout.cache
                / "persistent"
                / snapshot_id.removeprefix("sha256:")
                / consumer
                / identity
            )
            cache_path = final / "bars.parquet"
            reused = final.exists()
            if reused:
                _verify_cache(cache_path, digest, len(payload))
            else:
                with AtomicDirectory(self._layout.staging, final) as staging:
                    (staging.path / "bars.parquet").write_bytes(payload)
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
                    "cache_path": str(cache_path.resolve()),
                },
            )
            yield CacheUse(policy, cache_path.resolve(), CACHE_TRANSFORM_VERSION, evidence_path)
            return
        if policy != "ephemeral":
            raise ValueError(f"unsupported cache policy {policy!r}")
        ephemeral_root = self._layout.cache / "ephemeral" / run_id / f"{consumer}-{uuid4().hex}"
        ephemeral_root.mkdir(parents=True, exist_ok=False)
        cache_path = ephemeral_root / "bars.parquet"
        cache_path.write_bytes(payload)
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
            yield CacheUse(policy, cache_path.resolve(), CACHE_TRANSFORM_VERSION, evidence_path)
        finally:
            resolved = ephemeral_root.resolve()
            expected = (self._layout.cache / "ephemeral" / run_id).resolve()
            if expected not in resolved.parents:
                raise RuntimeError(f"ephemeral cache escaped expected root: {resolved}")
            shutil.rmtree(resolved)


def _cache_bytes(dataset: CanonicalDataset) -> bytes:
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
    ).replace_schema_metadata({b"transform_version": CACHE_TRANSFORM_VERSION.encode("utf-8")})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue().to_pybytes()


def _verify_cache(path: Path, digest: str, content_bytes: int) -> None:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MarketHubContractError(f"persistent cache is unreadable: {path}") from exc
    if len(payload) != content_bytes or sha256_bytes(payload) != digest:
        raise MarketHubContractError(f"persistent cache integrity mismatch: {path}")
    schema = pq.read_schema(pa.BufferReader(payload))
    version = (schema.metadata or {}).get(b"transform_version", b"").decode()
    if version != CACHE_TRANSFORM_VERSION:
        raise MarketHubContractError(f"persistent cache transform version mismatch: {path}")
