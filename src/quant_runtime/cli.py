from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from strategy_workspace import WorkspaceClient, WorkspaceWorker

from quant_runtime.conformance import RuntimeConformance
from quant_runtime.executor import RuntimeExecutor
from quant_runtime.preflight import RuntimePreflight

DEFAULT_WORKSPACE = Path(r"D:\WILL\STOCK\QuantResearch\runtime\workspace")


class CliUsageError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="quant-runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser(
        "preflight", help="validate and freeze a draft request without submitting a Workspace run"
    )
    preflight.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    preflight.add_argument("--request", type=Path, required=True)

    conformance = commands.add_parser(
        "conformance", help="observe a registered package against frozen synthetic fixtures"
    )
    conformance.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    conformance.add_argument("--request", type=Path, required=True)

    run = commands.add_parser("run", help="submit and execute a Workspace run request")
    run.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    run.add_argument("--package", type=Path)
    run.add_argument("--request", type=Path, required=True)

    retry = commands.add_parser("retry", help="create and execute a new attempt for a failed run")
    retry.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    retry.add_argument("--request-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "preflight":
            payload = _preflight(arguments.workspace, arguments.request)
            exit_code = 0 if payload["status"] == "accepted" else 1
        elif arguments.command == "conformance":
            payload = _conformance(arguments.workspace, arguments.request)
            exit_code = 0 if payload["status"] == "accepted" else 1
        elif arguments.command == "run":
            run = _run(arguments.workspace, arguments.request, arguments.package)
            payload = _stdout_payload(run)
            exit_code = 0 if run["status"] in {"completed", "rejected"} else 1
        else:
            run = _retry(arguments.workspace, arguments.request_id)
            payload = _stdout_payload(run)
            exit_code = 0 if run["status"] in {"completed", "rejected"} else 1
    except CliUsageError as exc:
        payload = {
            "status": "failed",
            "error": {
                "code": "quant_runtime_cli_usage",
                "message": str(exc),
                "exception_type": type(exc).__name__,
            },
        }
        exit_code = 2
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
    draft = _read_request(request_path)
    if package_path is not None:
        registered = client.register_package(package_path)
        draft["strategy_package"] = registered["package_ref"]
    if draft.get("schema") == "quant-research.runtime-preflight-request.v2":
        conformance = RuntimeConformance(client).conform(
            {
                "schema": "quant-research.runtime-conformance-request.v1",
                "strategy_package": draft["strategy_package"],
                "parameters": draft["parameters"],
                "sandbox_profile": draft["sandbox_profile"],
                "behavioral_scenarios": draft["behavioral_scenarios"],
            }
        )
        if conformance["status"] != "accepted":
            return _unsubmitted(conformance["observation"])
        draft = dict(draft)
        draft["behavioral_conformance"] = conformance["behavioral_conformance"]
        del draft["behavioral_scenarios"]
    preflight = RuntimePreflight(client).preflight(draft)
    if preflight["status"] != "accepted":
        return _unsubmitted(preflight["observation"])
    submitted = client.submit_run(_canonical_run_request(draft, preflight["frozen_snapshot"]))
    return RuntimeExecutor(client, worker).execute(str(submitted["run_id"]))


def _preflight(workspace: Path, request_path: Path) -> dict[str, Any]:
    return RuntimePreflight(WorkspaceClient(workspace)).preflight(_read_request(request_path))


def _conformance(workspace: Path, request_path: Path) -> dict[str, Any]:
    return RuntimeConformance(WorkspaceClient(workspace)).conform(_read_request(request_path))


def _canonical_run_request(draft: dict[str, Any], frozen_snapshot: Any) -> dict[str, Any]:
    if not isinstance(frozen_snapshot, dict):
        raise ValueError("accepted preflight lacks a frozen snapshot")
    sandboxed = draft.get("schema") == "quant-research.runtime-preflight-request.v2"
    request = {
        "schema": (
            "quant-research.workspace-run-request.v4"
            if sandboxed
            else "quant-research.workspace-run-request.v3"
        ),
        "strategy_package": draft["strategy_package"],
        "market_snapshot": frozen_snapshot,
        "parameters": draft["parameters"],
        "execution": draft["execution"],
    }
    if sandboxed:
        request["sandbox_profile"] = draft["sandbox_profile"]
        request["behavioral_conformance"] = draft["behavioral_conformance"]
    return request


def _unsubmitted(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": None,
        "status": "failed",
        "current_attempt_id": None,
        "result": None,
        "error": observation,
    }


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
        "request_id": run.get("run_id"),
        "attempt_id": run.get("current_attempt_id"),
        "result": run.get("result"),
        "error": run.get("error"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
