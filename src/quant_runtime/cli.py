from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from strategy_workspace import WorkspaceClient, WorkspaceWorker

from quant_runtime.executor import RuntimeExecutor

DEFAULT_WORKSPACE = Path(r"D:\WILL\STOCK\QuantResearch\runtime\workspace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="submit and execute a Workspace run request")
    run.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    run.add_argument("--package", type=Path)
    run.add_argument("--request", type=Path, required=True)

    retry = commands.add_parser("retry", help="create and execute a new attempt for a failed run")
    retry.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    retry.add_argument("--request-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "run":
            run = _run(arguments.workspace, arguments.request, arguments.package)
        else:
            run = _retry(arguments.workspace, arguments.request_id)
        payload = _stdout_payload(run)
        exit_code = 0 if run["status"] in {"completed", "rejected"} else 1
    except Exception as exc:
        payload = {
            "status": "failed",
            "error": {
                "code": "quant_runtime_cli_failed",
                "message": str(exc),
                "exception_type": type(exc).__name__,
            },
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


def _run(workspace: Path, request_path: Path, package_path: Path | None) -> dict[str, Any]:
    client = WorkspaceClient(workspace)
    worker = WorkspaceWorker(workspace)
    request = _read_request(request_path)
    if package_path is not None:
        registered = client.register_package(package_path)
        request["strategy_package"] = registered["package_ref"]
    submitted = client.submit_run(request)
    return RuntimeExecutor(client, worker).execute(str(submitted["run_id"]))


def _retry(workspace: Path, request_id: str) -> dict[str, Any]:
    client = WorkspaceClient(workspace)
    worker = WorkspaceWorker(workspace)
    retried = client.retry_run(request_id)
    return RuntimeExecutor(client, worker).execute(str(retried["run_id"]))


def _read_request(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run request {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("run request root must be an object")
    return value


def _stdout_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": run["status"],
        "request_id": run["run_id"],
        "attempt_id": run["current_attempt_id"],
        "result": run.get("result"),
        "error": run.get("error"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
