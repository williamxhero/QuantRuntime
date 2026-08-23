from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical import canonical_json, sha256_bytes, sha256_value
from .config import RunConfig
from .workflow import DiscoveryResult

MANIFEST_SCHEMA = "markethub-qlib.run-manifest.v1"


@dataclass(frozen=True, slots=True)
class WrittenRun:
    status: str
    run_id: str
    manifest_path: Path


def write_successful_run(config: RunConfig, result: DiscoveryResult, output: Path) -> WrittenRun:
    output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    paths.append(_write_csv(output / "qlib_signals.csv", result.signals))
    paths.append(_write_csv(output / "qlib_rank_ic.csv", result.ic.to_frame()))
    paths.append(_write_csv(output / "qlib_candidates.csv", result.candidates))
    paths.append(_write_csv(output / "qlib_risk_analysis.csv", result.risk))
    recorder = {
        "schema": "markethub-qlib.recorder-export.v1",
        "framework": "Qlib",
        "framework_version": result.metrics["framework_version"],
        "native_capabilities": [
            "qlib.contrib.evaluate_portfolio.get_rank_ic",
            "qlib.contrib.evaluate.risk_analysis",
        ],
        "strategy_spec": config.strategy_spec,
        "metrics": _deterministic_metrics(result.metrics),
        "data_lineage": {
            "data_version": result.dataset.data_version,
            "dataset_version": result.dataset.dataset_version,
            "canonical_input_hash": result.dataset.canonical_input_hash,
        },
    }
    paths.append(_write_json(output / "qlib_recorder_export.json", recorder))
    config_hash = sha256_value(config.raw)
    strategy_spec_hash = sha256_value(config.strategy_spec)
    run_id = _run_id(config_hash, strategy_spec_hash, result.dataset.canonical_input_hash)
    manifest = _manifest(
        run_id=run_id,
        framework_version=str(result.metrics["framework_version"]),
        status=result.status,
        data_version=result.dataset.data_version,
        dataset_version=result.dataset.dataset_version,
        config_hash=config_hash,
        strategy_spec_hash=strategy_spec_hash,
        canonical_input_hash=result.dataset.canonical_input_hash,
        artifacts=_artifact_records(output, paths),
        metrics=result.metrics,
    )
    manifest_path = _write_json(output / "run_manifest.json", manifest)
    return WrittenRun(status=result.status, run_id=run_id, manifest_path=manifest_path.resolve())


def write_failed_run(
    config: RunConfig,
    output: Path,
    error: Exception,
    *,
    framework_version: str,
) -> WrittenRun:
    output.mkdir(parents=True, exist_ok=True)
    error_path = _write_json(
        output / "failure.json",
        {
            "schema": "markethub-qlib.failure.v1",
            "error_type": type(error).__name__,
            "message": str(error),
        },
    )
    config_hash = sha256_value(config.raw)
    strategy_spec_hash = sha256_value(config.strategy_spec)
    run_id = _run_id(config_hash, strategy_spec_hash, "unavailable")
    manifest = _manifest(
        run_id=run_id,
        framework_version=framework_version,
        status="failed",
        data_version="unavailable",
        dataset_version="unavailable",
        config_hash=config_hash,
        strategy_spec_hash=strategy_spec_hash,
        canonical_input_hash="unavailable",
        artifacts=_artifact_records(output, [error_path]),
        metrics={},
    )
    manifest_path = _write_json(output / "run_manifest.json", manifest)
    return WrittenRun(status="failed", run_id=run_id, manifest_path=manifest_path.resolve())


def _manifest(
    *,
    run_id: str,
    framework_version: str,
    status: str,
    data_version: str,
    dataset_version: str,
    config_hash: str,
    strategy_spec_hash: str,
    canonical_input_hash: str,
    artifacts: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "framework": "Qlib",
        "framework_version": framework_version,
        "status": status,
        "data_version": data_version,
        "dataset_version": dataset_version,
        "config_hash": config_hash,
        "strategy_spec_hash": strategy_spec_hash,
        "canonical_input_hash": canonical_input_hash,
        "artifacts": artifacts,
        "metrics": metrics,
    }


def _artifact_records(root: Path, paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: item.name):
        payload = path.read_bytes()
        records.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(payload),
                "content_bytes": len(payload),
            }
        )
    return records


def _write_json(path: Path, value: Any) -> Path:
    path.write_bytes(canonical_json(value) + b"\n")
    return path


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    rendered = frame.to_csv(lineterminator="\n", float_format="%.12g")
    path.write_text(rendered, encoding="utf-8", newline="")
    return path


def _run_id(config_hash: str, strategy_spec_hash: str, input_hash: str) -> str:
    identity = sha256_value(
        {
            "config_hash": config_hash,
            "strategy_spec_hash": strategy_spec_hash,
            "canonical_input_hash": input_hash,
        }
    )
    return f"qlib-{identity[:20]}"


def _deterministic_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    stable = dict(metrics)
    fetch = stable.get("fetch")
    if isinstance(fetch, dict):
        stable["fetch"] = {
            key: value for key, value in fetch.items() if key != "fetch_seconds"
        }
    return stable
