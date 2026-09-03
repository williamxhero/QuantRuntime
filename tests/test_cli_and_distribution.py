from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
from conftest import PACKAGE, ROOT
from test_executor_topologies import request, snapshot

from quant_runtime import cli


def frozen_snapshot() -> dict:
    return {
        "schema": "quant-research.market-snapshot-ref.v2",
        "snapshot_id": "sha256:" + "a" * 64,
        "mode": "reference",
        "trust_policy": "verified_immutable",
        "source": {
            "adapter": "markethub",
            "adapter_version": "1.0.1",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
            "data_revision": "fixture-global-v1:fixture-daily-v1",
        },
        "query": {
            "instruments": ["SH.600000", "SZ.000001"],
            "start": "2025-01-01",
            "end": "2025-01-31",
            "frequency": "1d",
            "adjustment": "none",
        },
        "calendar": "cn-equity-v1",
        "contract_mapping": None,
        "as_of": "2025-02-01T00:00:00Z",
        "required_semantics": ["field_availability", "time"],
        "data_semantics": {
            "field_availability": {"status": "verified", "reason": "fixture"},
            "point_in_time": {"status": "not_evaluated", "reason": "fixture"},
            "time": {"status": "verified", "reason": "fixture"},
            "provider_lineage": {"status": "not_evaluated", "reason": "fixture"},
        },
        "verification": {
            "canonical_input_hash": "b" * 64,
            "data_version": "fixture-global-v1",
            "dataset_version": "fixture-daily-v1",
            "catalog_hash": "c" * 64,
            "calendar_hash": "d" * 64,
            "coverage_hash": "e" * 64,
        },
        "resolved_at": "2025-02-01T00:00:00Z",
    }


def test_cli_exposes_runtime_commands_with_strict_json_stdout(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    choices = next(
        action for action in cli.build_parser()._actions if action.dest == "command"
    ).choices
    assert set(choices) == {"preflight", "run", "retry"}

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
    monkeypatch.setattr(
        cli,
        "RuntimePreflight",
        lambda client: type(
            "Preflight",
            (),
            {
                "preflight": lambda self, draft: {
                    "status": "accepted",
                    "frozen_snapshot": frozen_snapshot(),
                }
            },
        )(),
    )
    request_path = tmp_path / "request.json"
    value = {
        "schema": "quant-research.runtime-preflight-request.v1",
        "strategy_package": {
            "schema": "quant-research.strategy-package-ref.v1",
            "strategy_id": "placeholder",
            "revision": 1,
            "package_hash": "0" * 64,
        },
        "snapshot_request": {},
        "parameters": {},
        "execution": request(
            {
                "schema": "quant-research.strategy-package-ref.v1",
                "strategy_id": "placeholder",
                "revision": 1,
                "package_hash": "0" * 64,
            },
            "formal_only",
        )["execution"],
    }
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


def test_cli_exposes_side_effect_free_preflight() -> None:
    choices = next(
        action for action in cli.build_parser()._actions if action.dest == "command"
    ).choices

    assert set(choices) == {"preflight", "run", "retry"}


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
    wheel = next(output.glob("quant_runtime-0.2.3-*.whl"))
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
