from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import nautilus_trader

from .config import RunConfig
from .engine import run_engine
from .manifest import write_manifest
from .markethub import MarketHubClient

BASE_ARTIFACTS = (
    "native_account.csv",
    "native_fills.csv",
    "native_orders.csv",
    "native_positions.csv",
    "native_statistics.json",
    "normalized_output.json",
)


def run(config_path: Path, output_dir: Path) -> tuple[dict[str, Any], Path]:
    config = RunConfig.load(config_path)
    client = MarketHubClient(config.data.base_url)
    dataset = client.fetch_dataset(
        config.data.instruments,
        config.data.start_date,
        config.data.end_date,
    )
    output = run_engine(dataset, config, output_dir)
    artifact_names = list(BASE_ARTIFACTS)
    if (output_dir / "strategy_decisions.json").exists():
        artifact_names.append("strategy_decisions.json")
    artifact_paths = [output_dir / name for name in artifact_names]
    return write_manifest(
        output_dir,
        framework_version=nautilus_trader.__version__,
        status="success",
        data_version=dataset.data_version,
        config_hash=config.config_hash,
        strategy_spec_hash=config.strategy.spec_hash,
        canonical_input_hash=dataset.input_hash,
        normalized_output_hash=output.output_hash,
        artifact_paths=artifact_paths,
        metrics={
            "fetch": client.metrics.as_dict(),
            "reference_decision_hash": output.decision_hash,
            "runtime": output.metrics,
        },
    )


def write_failed_run(
    config_path: Path,
    output_dir: Path,
    exc: Exception,
) -> tuple[dict[str, Any], Path]:
    config = RunConfig.load(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    error_path = output_dir / "run_error.json"
    error = {"message": str(exc), "type": type(exc).__name__}
    error_path.write_text(
        json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return write_manifest(
        output_dir,
        framework_version=nautilus_trader.__version__,
        status="failed",
        data_version=None,
        config_hash=config.config_hash,
        strategy_spec_hash=config.strategy.spec_hash,
        canonical_input_hash=None,
        normalized_output_hash=None,
        artifact_paths=[error_path],
        error=error,
    )
