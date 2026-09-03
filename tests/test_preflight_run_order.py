from __future__ import annotations

import json
from pathlib import Path

from quant_runtime import cli


def test_run_does_not_submit_when_preflight_fails(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []

    class Client:
        def __init__(self, workspace):
            del workspace

        def submit_run(self, request):
            del request
            events.append("submit_run")
            raise AssertionError("failed preflight must not submit")

    class Worker:
        def __init__(self, workspace):
            del workspace

    class Preflight:
        def __init__(self, client):
            del client

        def preflight(self, draft):
            del draft
            events.append("preflight")
            return {
                "status": "failed",
                "observation": {
                    "classification": "market_data_incident",
                    "code": "coverage_incomplete",
                    "message": "coverage is incomplete",
                },
            }

    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimePreflight", Preflight)

    request_path = tmp_path / "draft.json"
    request_path.write_text("{}", encoding="utf-8")
    result = cli._run(tmp_path / "workspace", request_path, None)

    assert events == ["preflight"]
    assert result == {
        "run_id": None,
        "status": "failed",
        "current_attempt_id": None,
        "result": None,
        "error": {
            "classification": "market_data_incident",
            "code": "coverage_incomplete",
            "message": "coverage is incomplete",
        },
    }


def test_run_submits_the_preflight_snapshot_before_execution(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []
    frozen_snapshot = {"snapshot_id": "sha256:" + "f" * 64}

    class Client:
        def __init__(self, workspace):
            del workspace

        def submit_run(self, request):
            events.append("submit_run")
            assert request["market_snapshot"] == frozen_snapshot
            return {"run_id": "run-frozen"}

    class Worker:
        def __init__(self, workspace):
            del workspace

    class Preflight:
        def __init__(self, client):
            del client

        def preflight(self, draft):
            del draft
            events.append("preflight")
            return {"status": "accepted", "frozen_snapshot": frozen_snapshot}

    class Executor:
        def __init__(self, client, worker):
            del client, worker

        def execute(self, run_id):
            events.append("start_attempt_and_nautilus")
            assert run_id == "run-frozen"
            return {
                "run_id": run_id,
                "status": "completed",
                "current_attempt_id": "attempt-frozen",
                "result": {"outcome": "completed"},
                "error": None,
            }

    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimePreflight", Preflight)
    monkeypatch.setattr(cli, "RuntimeExecutor", Executor)

    request_path = tmp_path / "draft.json"
    request_path.write_text(
        json.dumps(
            {
                "strategy_package": {"package_hash": "a" * 64},
                "parameters": {},
                "execution": {"topology": "formal_only", "formal": []},
            }
        ),
        encoding="utf-8",
    )
    result = cli._run(tmp_path / "workspace", request_path, None)

    assert events == ["preflight", "submit_run", "start_attempt_and_nautilus"]
    assert result["status"] == "completed"


def test_sandboxed_run_conforms_before_preflight_and_freezes_the_pass_ref(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []
    conformance_ref = {"conformance_id": "sha256:" + "c" * 64}

    class Client:
        def __init__(self, workspace):
            del workspace

        def register_package(self, package_path):
            events.append("register_package")
            assert package_path == tmp_path / "generated-package"
            return {"package_ref": {"package_hash": "registered"}}

        def submit_run(self, request):
            events.append("submit_run")
            assert request["behavioral_conformance"] == conformance_ref
            assert request["schema"] == "quant-research.workspace-run-request.v4"
            return {"run_id": "sandboxed"}

    class Worker:
        def __init__(self, workspace):
            del workspace

    class Conformance:
        def __init__(self, client):
            del client

        def conform(self, request):
            events.append("conformance")
            assert "behavioral_scenarios" in request
            assert request["strategy_package"] == {"package_hash": "registered"}
            return {"status": "accepted", "behavioral_conformance": conformance_ref}

    class Preflight:
        def __init__(self, client):
            del client

        def preflight(self, draft):
            events.append("preflight")
            assert draft["behavioral_conformance"] == conformance_ref
            return {"status": "accepted", "frozen_snapshot": {"snapshot_id": "frozen"}}

    class Executor:
        def __init__(self, client, worker):
            del client, worker

        def execute(self, run_id):
            events.append("attempt")
            return {
                "run_id": run_id,
                "status": "completed",
                "current_attempt_id": "attempt",
                "result": {"outcome": "completed"},
                "error": None,
            }

    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimeConformance", Conformance)
    monkeypatch.setattr(cli, "RuntimePreflight", Preflight)
    monkeypatch.setattr(cli, "RuntimeExecutor", Executor)
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "quant-research.runtime-preflight-request.v2",
                "strategy_package": {},
                "snapshot_request": {},
                "parameters": {},
                "sandbox_profile": {},
                "behavioral_scenarios": [{}],
                "execution": {},
            }
        ),
        encoding="utf-8",
    )

    result = cli._run(
        tmp_path / "workspace",
        request_path,
        tmp_path / "generated-package",
    )

    assert result["status"] == "completed"
    assert events == [
        "register_package",
        "conformance",
        "preflight",
        "submit_run",
        "attempt",
    ]


def test_sandboxed_run_rejection_has_zero_preflight_submission_or_attempt_calls(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class Client:
        def __init__(self, workspace):
            del workspace

        def submit_run(self, request):
            del request
            raise AssertionError("rejected conformance must not submit")

    class Worker:
        def __init__(self, workspace):
            del workspace

    class Conformance:
        def __init__(self, client):
            del client

        def conform(self, request):
            del request
            events.append("conformance")
            return {
                "status": "rejected",
                "observation": {
                    "classification": "strategy_rejection",
                    "code": "behavioral_conformance_failed",
                    "message": "fixture",
                },
            }

    class Forbidden:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("rejected conformance must stop before preflight or execution")

    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimeConformance", Conformance)
    monkeypatch.setattr(cli, "RuntimePreflight", Forbidden)
    monkeypatch.setattr(cli, "RuntimeExecutor", Forbidden)
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "quant-research.runtime-preflight-request.v2",
                "strategy_package": {},
                "snapshot_request": {},
                "parameters": {},
                "sandbox_profile": {},
                "behavioral_scenarios": [{}],
                "execution": {},
            }
        ),
        encoding="utf-8",
    )

    result = cli._run(tmp_path / "workspace", request_path, None)

    assert result["status"] == "failed"
    assert result["error"]["classification"] == "strategy_rejection"
    assert events == ["conformance"]
