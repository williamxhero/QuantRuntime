from __future__ import annotations

from typing import Any

import pandas as pd
import qlib

from quant_runtime.adapters.interface import (
    DiscoveryAdapterResult,
    DiscoveryRunInput,
)
from quant_runtime.contracts.canonical_hash import artifact_records, sha256_value, write_json
from quant_runtime.discovery.qlib.qlib_loader import load_frame
from quant_runtime.sdk.entrypoint import load_package_entrypoint

ADAPTER_VERSION = "1.0.0"


class QlibDiscoveryAdapter:
    name = "qlib"
    adapter_version = ADAPTER_VERSION
    engine_version = qlib.__version__

    def run(self, value: DiscoveryRunInput) -> DiscoveryAdapterResult:
        if value.snapshot.dataset is None:
            raise ValueError("Qlib discovery requires a verified snapshot read")
        entrypoint = value.package.resolve_entrypoint("discovery", self.name)
        discover = load_package_entrypoint(value.package.root, entrypoint)
        result = discover(load_frame(value.snapshot.dataset), value.parameters)
        if not isinstance(result, dict):
            raise TypeError("Qlib package entrypoint must return an artifact mapping")
        value.output.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in ("signals", "rank_ic", "candidates", "risk"):
            frame = result.get(name)
            if isinstance(frame, pd.Series):
                frame = frame.to_frame()
            if not isinstance(frame, pd.DataFrame):
                raise TypeError(f"Qlib package result {name!r} must be a DataFrame or Series")
            path = value.output / f"qlib_{name}.csv"
            path.write_text(
                frame.to_csv(lineterminator="\n", float_format="%.12g"),
                encoding="utf-8",
                newline="",
            )
            paths.append(path)
        manifest = {
            "schema": "quant-research.discovery-artifact.v1",
            "backend_id": self.name,
            "adapter_version": self.adapter_version,
            "engine_version": self.engine_version,
            "strategy_package_hash": value.package.package_hash,
            "parameters_hash": value.package.parameters_hash(value.parameters),
            "snapshot_id": value.snapshot.snapshot_id,
            "artifacts": artifact_records(value.output, paths),
        }
        manifest["artifact_hash"] = sha256_value(manifest)
        manifest_path = write_json(value.output / "discovery_manifest.json", manifest)
        evidence = tuple(artifact_records(value.output, [*paths, manifest_path]))
        candidates = result["candidates"]
        rank_ic = result["rank_ic"]
        metrics: dict[str, Any] = {
            "candidate_rows": len(candidates),
            "mean_rank_ic": float(rank_ic.mean()) if len(rank_ic) else None,
        }
        return DiscoveryAdapterResult(
            self.name,
            self.adapter_version,
            self.engine_version,
            str(manifest["artifact_hash"]),
            metrics,
            evidence,
        )
