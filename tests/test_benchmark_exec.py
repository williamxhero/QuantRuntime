from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from quant_runtime.artifacts import sha256_value
from quant_runtime.benchmark import BenchmarkExecutionService
from quant_runtime.cli import main
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation


class ContractBackend:
    production = False

    def __init__(self) -> None:
        self.calls: list[PreparedSandboxInvocation] = []

    def invoke(self, prepared: PreparedSandboxInvocation) -> dict[str, Any]:
        self.calls.append(prepared)
        assert prepared.package.root.joinpath("factor.py").read_text(encoding="utf-8")
        assert prepared.inputs.joinpath("fixture.json").is_file()
        return {
            "schema": "quant-runtime.sandbox-worker-result.v2",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "success",
            "payload": {"compiled": True, "outputs": [None, None, -0.1]},
            "sandbox": {"contract": "fake"},
        }


def _write_inputs(root: Path, *, mode: str = "contract_fake") -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "transport-source.py"
    fixture = root / "transport-fixture.json"
    request = root / "request.json"
    source_bytes = b"def evaluate(rows):\n    return [None for _ in rows]\n"
    fixture_bytes = b'{"rows":[{"close":10.0},{"close":11.0},{"close":10.0}]}\n'
    source.write_bytes(source_bytes)
    fixture.write_bytes(fixture_bytes)
    body = {
        "schema": "quant-runtime.benchmark-exec-request.v1",
        "execution_mode": mode,
        "source": {
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "bytes": len(source_bytes),
            "media_type": "text/x-python",
        },
        "fixture": {
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "bytes": len(fixture_bytes),
            "media_type": "application/json",
        },
        "entrypoint": "factor.py:evaluate",
        "sandbox_profile": {},
    }
    request.write_text(json.dumps({**body, "invocation_id": sha256_value(body)}), encoding="utf-8")
    return request, source, fixture


def test_transport_only_execution_is_content_bound_and_path_independent(tmp_path: Path) -> None:
    first_paths = _write_inputs(tmp_path / "first")
    second_paths = _write_inputs(tmp_path / "second")
    first_backend = ContractBackend()
    second_backend = ContractBackend()

    first = BenchmarkExecutionService(first_backend).execute(*first_paths)
    second = BenchmarkExecutionService(second_backend).execute(*second_paths)

    assert first == second
    assert first["status"] == "completed"
    assert first["classification"] == "success"
    assert first["payload"] == {"compiled": True, "outputs": [None, None, -0.1]}
    assert not ({"run_id", "request_id", "evidence", "strategy_package"} & set(first))
    assert len(first_backend.calls) == len(second_backend.calls) == 1


def test_transport_digest_drift_fails_before_sandbox_invocation(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path)
    paths[1].write_text("tampered", encoding="utf-8")
    backend = ContractBackend()

    with pytest.raises(ValueError, match="source identity mismatch"):
        BenchmarkExecutionService(backend).execute(*paths)

    assert backend.calls == []


def test_entrypoint_requires_one_python_transport_file(tmp_path: Path) -> None:
    request, source, fixture = _write_inputs(tmp_path)
    payload = json.loads(request.read_text(encoding="utf-8"))
    payload["entrypoint"] = "factor.txt:evaluate"
    identity = {key: value for key, value in payload.items() if key != "invocation_id"}
    payload["invocation_id"] = sha256_value(identity)
    request.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="entrypoint"):
        BenchmarkExecutionService(ContractBackend()).execute(request, source, fixture)


class OversizedResultBackend(ContractBackend):
    def invoke(self, prepared: PreparedSandboxInvocation) -> dict[str, Any]:
        result = super().invoke(prepared)
        result["payload"] = {"compiled": True, "outputs": ["x" * (2**21)]}
        return result


def test_worker_result_is_bounded_before_transport_return(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path)

    with pytest.raises(ValueError, match="bounded"):
        BenchmarkExecutionService(OversizedResultBackend()).execute(*paths)


def test_production_mode_requires_a_production_attested_backend(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, mode="production_attested_oci")
    backend = ContractBackend()

    result = BenchmarkExecutionService(backend).execute(*paths)

    assert result["status"] == "blocked"
    assert result["classification"] == "policy_rejection"
    assert result["error"] == {"code": "benchmark_oci_unavailable"}
    assert backend.calls == []


def test_cli_exposes_strict_benchmark_exec_without_workspace_run_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request, source, fixture = _write_inputs(tmp_path, mode="production_attested_oci")

    assert (
        main(
            [
                "benchmark-exec",
                "--request",
                str(request),
                "--source",
                str(source),
                "--fixture",
                str(fixture),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert payload["status"] == "blocked"
    assert payload["error"]["code"] == "benchmark_oci_unavailable"
    assert not ({"run_id", "request_id", "evidence", "strategy_package"} & set(payload))
