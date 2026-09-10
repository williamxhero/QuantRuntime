from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import qlib

from quant_runtime.adapters.discovery.qlib.loader import load_frame
from quant_runtime.adapters.interface import (
    DiscoveryAdapterResult,
    DiscoveryRunInput,
)
from quant_runtime.artifacts import artifact_records, sha256_value, write_json
from quant_runtime.entrypoint import load_package_entrypoint

ADAPTER_VERSION = "1.0.0"


class QlibStrategyError(ValueError):
    """The package-owned Qlib entrypoint rejected or violated its contract."""


class QlibDiscoveryAdapter:
    name = "qlib"
    adapter_version = ADAPTER_VERSION
    engine_version = qlib.__version__

    def run(self, value: DiscoveryRunInput) -> DiscoveryAdapterResult:
        if value.snapshot.dataset is None:
            raise ValueError("Qlib discovery requires a verified snapshot read")
        entrypoint = value.package.resolve_entrypoint("discovery", self.name)
        return run_qlib_discovery_frame(
            package_root=value.package.root,
            entrypoint=entrypoint,
            package_hash=value.package.package_hash,
            parameters=value.parameters,
            snapshot_id=value.snapshot.snapshot_id,
            frame=load_frame(value.snapshot.dataset),
            output=value.output,
        )


def run_qlib_discovery_frame(
    *,
    package_root: Path,
    entrypoint: str,
    package_hash: str,
    parameters: dict[str, Any],
    snapshot_id: str,
    frame: pd.DataFrame,
    output: Path,
) -> DiscoveryAdapterResult:
    try:
        discover = load_package_entrypoint(package_root, entrypoint)
        result = discover(frame, parameters)
    except Exception as exc:
        raise QlibStrategyError("Qlib package entrypoint rejected execution") from exc
    try:
        if not isinstance(result, dict):
            raise TypeError("Qlib package entrypoint must return an artifact mapping")
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in ("signals", "rank_ic", "candidates", "risk"):
            frame = result.get(name)
            if isinstance(frame, pd.Series):
                frame = frame.to_frame()
            if not isinstance(frame, pd.DataFrame):
                raise TypeError(f"Qlib package result {name!r} must be a DataFrame or Series")
            path = output / f"qlib_{name}.csv"
            path.write_text(
                frame.to_csv(lineterminator="\n", float_format="%.12g"),
                encoding="utf-8",
                newline="",
            )
            paths.append(path)
        manifest = {
            "schema": "quant-research.discovery-artifact.v1",
            "backend_id": "qlib",
            "adapter_version": ADAPTER_VERSION,
            "engine_version": qlib.__version__,
            "strategy_package_hash": package_hash,
            "parameters_hash": sha256_value(parameters),
            "snapshot_id": snapshot_id,
            "artifacts": artifact_records(output, paths),
        }
        manifest["artifact_hash"] = sha256_value(manifest)
        manifest_path = write_json(output / "discovery_manifest.json", manifest)
        evidence = tuple(artifact_records(output, [*paths, manifest_path]))
        candidates = result["candidates"]
        rank_ic = result["rank_ic"]
        metrics: dict[str, Any] = {
            "candidate_rows": len(candidates),
            "mean_rank_ic": float(rank_ic.mean()) if len(rank_ic) else None,
        }
        return DiscoveryAdapterResult(
            "qlib",
            ADAPTER_VERSION,
            qlib.__version__,
            str(manifest["artifact_hash"]),
            metrics,
            evidence,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise QlibStrategyError("Qlib package result violated its contract") from exc
