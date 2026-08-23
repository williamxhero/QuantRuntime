from __future__ import annotations

from pathlib import Path

from quant_runtime.contracts.canonical_hash import read_json
from quant_runtime.workspace import StrategyWorkspace, validate_package

from .result import ApplicationResult


def run_package_validate(
    package_path: Path,
    parameters_path: Path | None = None,
) -> ApplicationResult:
    parameters = read_json(parameters_path) if parameters_path is not None else None
    package = validate_package(package_path, parameters)
    return ApplicationResult(
        payload={
            "status": "valid",
            "strategy_id": package.strategy_id,
            "revision": package.revision,
            "package_hash": package.package_hash,
            "parameters_hash": package.parameters_hash(parameters),
        },
        exit_code=0,
    )


def run_snapshot_resolve(request_path: Path, runtime_root: Path) -> ApplicationResult:
    raw = read_json(request_path)
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        raise ValueError("snapshot request data must be an object")
    snapshot = StrategyWorkspace(runtime_root).resolve_snapshot(data)
    return ApplicationResult(
        payload={
            "status": "resolved",
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_mode": snapshot.mode,
            "manifest_path": str(snapshot.manifest_path),
        },
        exit_code=0,
    )


def run_workspace(request_path: Path, runtime_root: Path) -> ApplicationResult:
    request = read_json(request_path)
    manifest, path = StrategyWorkspace(runtime_root).run(
        request,
        request_root=request_path.resolve().parent,
    )
    return ApplicationResult(
        payload={
            "status": manifest["status"],
            "run_id": manifest["run_id"],
            "manifest_path": str(path),
        },
        exit_code=0 if manifest["status"] == "completed" else 1,
    )
