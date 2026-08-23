from __future__ import annotations

from pathlib import Path

from quant_runtime.formal import get_formal_runtime

from .result import ApplicationResult


def run_evaluate(
    candidate_manifest: Path,
    config: Path,
    output: Path,
    *,
    runtime_name: str = "nautilus",
) -> ApplicationResult:
    runtime = get_formal_runtime(runtime_name)
    manifest, path = runtime.evaluate(candidate_manifest, config, output)
    return ApplicationResult(
        payload={
            "status": manifest["status"],
            "run_id": manifest["run_id"],
            "manifest_path": str(path),
        },
        exit_code=0 if manifest["status"] == "matched" else 1,
    )
