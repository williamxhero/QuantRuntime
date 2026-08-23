from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import qlib

from quant_runtime.contracts.candidate_manifest import CANDIDATE_SCHEMA
from quant_runtime.contracts.canonical_hash import (
    artifact_records,
    sha256_value,
    write_json,
)
from quant_runtime.semantics.decision_record import decision_envelope, decision_hash

from .candidate_builder import candidate_decisions
from .workflow import DiscoveryConfig, DiscoveryResult


def write_candidate_run(
    config: DiscoveryConfig,
    result: DiscoveryResult,
    output: Path,
) -> tuple[dict[str, Any], Path]:
    output.mkdir(parents=True, exist_ok=True)
    envelope = decision_envelope(candidate_decisions(result.candidates), config.strategy.spec_hash)
    reference_hash = decision_hash(envelope)
    paths = [
        write_json(output / "strategy_spec.json", config.strategy.payload),
        write_json(output / "strategy_decisions.json", envelope),
        _write_csv(output / "qlib_signals.csv", result.signals),
        _write_csv(output / "qlib_rank_ic.csv", result.rank_ic.to_frame()),
        _write_csv(output / "qlib_candidates.csv", result.candidates),
        _write_csv(output / "qlib_risk_analysis.csv", result.risk),
    ]
    stable_metrics = _stable_metrics(result.metrics)
    recorder = {
        "schema": "quant-runtime.qlib-recorder-export.v1",
        "framework": "Qlib",
        "framework_version": qlib.__version__,
        "native_capabilities": [
            "qlib.contrib.evaluate_portfolio.get_rank_ic",
            "qlib.contrib.evaluate.risk_analysis",
        ],
        "strategy_spec": config.strategy.payload,
        "metrics": stable_metrics,
        "data_lineage": {
            "data_version": result.dataset.data_version,
            "dataset_version": result.dataset.dataset_version,
            "canonical_input_hash": result.dataset.input_hash,
        },
    }
    paths.append(write_json(output / "qlib_recorder_export.json", recorder))
    run_id = (
        "qr-discover-"
        + sha256_value(
            {
                "config_hash": config.config_hash,
                "strategy_spec_hash": config.strategy.spec_hash,
                "canonical_input_hash": result.dataset.input_hash,
            }
        )[:24]
    )
    metrics = dict(result.metrics)
    metrics["reference_decision_hash"] = reference_hash
    manifest = {
        "schema": CANDIDATE_SCHEMA,
        "run_id": run_id,
        "framework": "Qlib",
        "framework_version": qlib.__version__,
        "status": result.status,
        "data_version": result.dataset.data_version,
        "dataset_version": result.dataset.dataset_version,
        "config_hash": config.config_hash,
        "strategy_spec_hash": config.strategy.spec_hash,
        "canonical_input_hash": result.dataset.input_hash,
        "artifacts": artifact_records(output, paths),
        "metrics": metrics,
    }
    path = write_json(output / "candidate_manifest.json", manifest).resolve()
    return manifest, path


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    path.write_text(
        frame.to_csv(lineterminator="\n", float_format="%.12g"),
        encoding="utf-8",
        newline="",
    )
    return path


def _stable_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    stable = dict(metrics)
    fetch = stable.get("fetch")
    if isinstance(fetch, dict):
        stable["fetch"] = {key: value for key, value in fetch.items() if key != "fetch_seconds"}
    return stable
