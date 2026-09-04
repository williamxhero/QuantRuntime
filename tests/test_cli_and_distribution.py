from __future__ import annotations

import hashlib
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
    assert set(choices) == {
        "capabilities",
        "conformance",
        "preflight",
        "sandbox-proof",
        "run",
        "retry",
    }

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

    assert set(choices) == {
        "capabilities",
        "conformance",
        "preflight",
        "sandbox-proof",
        "run",
        "retry",
    }


def _canonical_sha256(value: object) -> str:
    content = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(content).hexdigest()


def test_capabilities_are_deterministic_strict_and_side_effect_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Forbidden:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("capability discovery has no external owner access")

    monkeypatch.setattr(cli, "WorkspaceClient", Forbidden)
    monkeypatch.setattr(cli, "RuntimePreflight", Forbidden)
    monkeypatch.setattr(cli, "RuntimeExecutor", Forbidden)

    assert cli.main(["capabilities"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    identity = {key: value for key, value in payload.items() if key != "capability_id"}

    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert payload["schema"] == "quant-runtime.cli-capabilities.v1"
    assert payload["runtime_version"] == "0.2.4"
    assert payload["cli_protocol"] == "quant-runtime.cli.v1"
    assert payload["capabilities"] == ["frozen-preflight.v1", "preflight.v1", "run.v1"]
    assert payload["capability_id"] == _canonical_sha256(identity)


def test_frozen_run_uses_existing_result_without_a_second_live_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_ref = {
        "schema": "quant-research.strategy-package-ref.v1",
        "strategy_id": "fixture",
        "revision": 1,
        "package_hash": "1" * 64,
    }
    request_value = {
        "schema": "quant-research.runtime-preflight-request.v3",
        "strategy_package": package_ref,
        "snapshot_request": {
            "adapter": "markethub",
            "snapshot_mode": "reference",
            "trust_policy": "verified_immutable",
            "local_cache": "none",
            "endpoint_contract": "v2",
            "base_url": "http://fixture",
            "as_of": "2025-02-01T00:00:00Z",
            "required_semantics": ["field_availability", "time"],
            "query": {
                "instruments": ["SH.600000", "SZ.000001"],
                "start": "2025-01-01",
                "end": "2025-01-31",
                "frequency": "1d",
                "adjustment": "none",
                "calendar": "cn-equity-v1",
                "contract_mapping": None,
            },
        },
        "parameters": {},
        "sandbox_profile": {},
        "behavioral_conformance": {},
        "execution": request(package_ref, "formal_only")["execution"],
    }
    result_value = {
        "schema": "quant-research.runtime-preflight-result.v1",
        "status": "accepted",
        "frozen_snapshot": frozen_snapshot(),
        "evidence": {
            "strategy_package": request_value["strategy_package"],
            "verification": frozen_snapshot()["verification"],
            "data_semantics": frozen_snapshot()["data_semantics"],
            "behavioral_conformance": request_value["behavioral_conformance"],
        },
    }
    capability = cli.runtime_capabilities()
    binding_identity = {
        "schema": "quant-runtime.validation-binding.v1",
        "protocol_id": "2" * 64,
        "cell_id": "3" * 64,
        "request_sha256": _canonical_sha256(request_value),
        "preflight_sha256": _canonical_sha256(result_value),
        "runtime_capability_id": capability["capability_id"],
    }
    binding = {**binding_identity, "binding_id": _canonical_sha256(binding_identity)}
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "preflight.json"
    binding_path = tmp_path / "binding.json"
    request_path.write_text(json.dumps(request_value), encoding="utf-8")
    result_path.write_text(json.dumps(result_value), encoding="utf-8")
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    events: list[str] = []

    class Client:
        def __init__(self, root):
            del root

        def submit_run(self, canonical):
            events.append("submit")
            assert canonical["market_snapshot"] == result_value["frozen_snapshot"]
            return {"run_id": "run-1"}

    class Worker:
        def __init__(self, root):
            del root

    class Executor:
        def __init__(self, client, worker):
            del client, worker

        def execute(self, run_id):
            events.append("execute")
            return {"run_id": run_id, "status": "completed"}

    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimeExecutor", Executor)
    monkeypatch.setattr(
        cli,
        "RuntimePreflight",
        lambda client: (_ for _ in ()).throw(AssertionError("second live preflight")),
    )
    monkeypatch.setattr(cli, "validate_frozen_preflight", lambda client, draft, result: None)
    monkeypatch.setattr(cli, "validate_frozen_transport", lambda draft, result: None)

    assert cli.main(
        [
            "run",
            "--workspace",
            str(tmp_path / "workspace"),
            "--request",
            str(request_path),
            "--frozen-preflight",
            str(result_path),
            "--validation-binding",
            str(binding_path),
        ]
    ) == 0

    assert json.loads(capsys.readouterr().out)["request_id"] == "run-1"
    assert events == ["submit", "execute"]


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
    wheel = next(output.glob("quant_runtime-0.2.4-*.whl"))
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode()
    assert "Requires-Dist: strategy-workspace<0.3,>=0.1.0" in metadata
    assert not any("quant_runtime/workspace/" in name for name in names)
    assert not any("quant_runtime/schemas/" in name for name in names)
    assert not any("candidate_manifest" in name or "formal_manifest" in name for name in names)
    assert not any(name.startswith("strategies/") or name.startswith("configs/") for name in names)


def test_request_fixture_uses_complete_snapshot_contract() -> None:
    assert snapshot()["source"]["data_revision"]
