from __future__ import annotations

from pathlib import Path

from quant_runtime.discovery.qlib import DiscoveryConfig, run_discovery, write_candidate_run

from .result import ApplicationResult


def run_discover(config_path: Path, output: Path) -> ApplicationResult:
    config = DiscoveryConfig.load(config_path)
    result = run_discovery(config)
    manifest, path = write_candidate_run(config, result, output)
    return ApplicationResult(
        payload={
            "status": manifest["status"],
            "run_id": manifest["run_id"],
            "manifest_path": str(path),
        },
        exit_code=0 if manifest["status"] == "passed" else 1,
    )
