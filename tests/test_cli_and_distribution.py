from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
from conftest import PACKAGE, ROOT
from test_executor_topologies import request, snapshot

from quant_runtime import cli


def test_cli_only_exposes_run_and_retry_with_strict_json_stdout(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    choices = next(
        action for action in cli.build_parser()._actions if action.dest == "command"
    ).choices
    assert set(choices) == {"run", "retry"}

    class Executor:
        def __init__(self, client, worker):
            del client, worker

        def execute(self, request_id):
            return {
                "run_id": request_id,
                "status": "completed",
                "current_attempt_id": "attempt-1",
                "result": {"outcome": "completed"},
                "error": None,
            }

    monkeypatch.setattr(cli, "RuntimeExecutor", Executor)
    request_path = tmp_path / "request.json"
    value = request(
        {
            "schema": "quant-research.strategy-package-ref.v1",
            "strategy_id": "placeholder",
            "revision": 1,
            "package_hash": "0" * 64,
        },
        "formal_only",
    )
    request_path.write_text(json.dumps(value), encoding="utf-8")
    assert (
        cli.main(
            [
                "run",
                "--workspace",
                str(tmp_path / "workspace"),
                "--package",
                str(PACKAGE),
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert json.loads(output) == {
        "status": "completed",
        "request_id": json.loads(output)["request_id"],
        "attempt_id": "attempt-1",
        "result": {"outcome": "completed"},
        "error": None,
    }


@pytest.mark.parametrize(
    "arguments",
    [[], ["unknown"], ["run"], ["retry", "--request-id", "run-1", "--bogus"]],
)
def test_cli_usage_errors_are_one_json_document(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "quant_runtime_cli_usage"


def test_cli_help_remains_normal_text(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert captured.out.startswith("usage: quant-runtime")
    assert captured.err == ""


def test_wheel_contains_only_runtime_execution_ownership(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("quant_runtime-0.2.1-*.whl"))
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode()
    assert "Requires-Dist: strategy-workspace" in metadata
    assert not any("quant_runtime/workspace/" in name for name in names)
    assert not any("quant_runtime/schemas/" in name for name in names)
    assert not any("candidate_manifest" in name or "formal_manifest" in name for name in names)
    assert not any(name.startswith("strategies/") or name.startswith("configs/") for name in names)


def test_request_fixture_uses_complete_snapshot_contract() -> None:
    assert snapshot()["source"]["data_revision"]
