from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import quant_runtime.sandbox.worker as worker
from quant_runtime.sandbox import CancellationToken
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation
from quant_runtime.sandbox.oci import (
    BACKEND_ID,
    BACKEND_IMPLEMENTATION,
    CONTROL_MOUNT,
    CONTROL_RESULT,
    MECHANISM,
    MECHANISM_VERSION,
    PRODUCTION_PROCESS_LIMIT,
    OciSandboxBackend,
    OciSandboxConfig,
)


@pytest.mark.parametrize("phase, target", [("discovery", "_discovery"), ("formal", "_formal")])
def test_candidate_routes_native_output_to_explicit_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, target: str
) -> None:
    protocol_path = tmp_path / "invocation.json"
    result_path = tmp_path / "control" / "sandbox-result.json"
    output_path = tmp_path / "output"
    result_path.parent.mkdir()
    output_path.mkdir()
    protocol_path.write_text(
        json.dumps({"phase": phase, "invocation_id": "sha256:" + "a" * 64}),
        encoding="utf-8",
    )
    observed: dict[str, Path] = {}

    def fake_handler(protocol: dict, result: Path, output: Path) -> int:
        observed.update(protocol=protocol, result=result, output=output)
        return 17

    monkeypatch.setattr(worker, target, fake_handler)

    assert worker._candidate([str(protocol_path), str(result_path), str(output_path)]) == 17
    assert observed["result"] == result_path
    assert observed["output"] == output_path


def test_candidate_keeps_legacy_output_path_when_argument_is_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol_path = tmp_path / "invocation.json"
    result_path = tmp_path / "control" / "sandbox-result.json"
    result_path.parent.mkdir()
    protocol_path.write_text(
        json.dumps({"phase": "formal", "invocation_id": "sha256:" + "b" * 64}),
        encoding="utf-8",
    )
    observed: list[Path] = []
    monkeypatch.setattr(
        worker,
        "_formal",
        lambda _protocol, _result, output: observed.append(output) or 0,
    )

    assert worker._candidate([str(protocol_path), str(result_path)]) == 0
    assert observed == [result_path.parent]


def test_oci_worker_command_separates_control_result_and_native_output(tmp_path: Path) -> None:
    image = "sha256:" + "c" * 64
    invocation_id = "sha256:" + "d" * 64
    proof = {
        "proof_id": "sha256:" + "e" * 64,
        "dependency_lock_identity": "sha256:" + "f" * 64,
    }
    limits = {
        "cpu_seconds": 10,
        "memory_bytes": 64 * 1024**2,
        "wall_clock_seconds": 10,
        "processes": PRODUCTION_PROCESS_LIMIT,
        "filesystem_bytes": 10 * 1024**2,
        "stdout_bytes": 1024,
        "stderr_bytes": 1024,
        "artifacts": 4,
    }
    profile = {
        "containment": {
            "backend_id": BACKEND_ID,
            "implementation": BACKEND_IMPLEMENTATION,
            "mechanism": MECHANISM,
            "mechanism_version": MECHANISM_VERSION,
            "platform": "linux",
            "proof": proof["proof_id"],
        },
        "dependency_environment": {
            "kind": "oci-image",
            "identity": image,
            "lock_identity": proof["dependency_lock_identity"],
        },
        "capabilities": {"network": "deny", "filesystem": "sealed", "subprocess": "bounded"},
        "limits": limits,
    }
    package = tmp_path / "package"
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    package.mkdir()
    inputs.mkdir()
    output.mkdir()
    prepared = PreparedSandboxInvocation(
        {"invocation_id": invocation_id, "sandbox_profile": profile},
        SimpleNamespace(root=package),
        inputs,
        output,
        CancellationToken(),
    )
    backend = object.__new__(OciSandboxBackend)
    backend.config = OciSandboxConfig(image=image)
    backend._docker = "docker"
    calls: list[tuple[str, ...]] = []

    def control(*arguments: str, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
        del timeout, check
        calls.append(arguments)
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(arguments, 0, '{"Running":false,"Pid":0}', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def export(_name: str, destination: Path, *, maximum_bytes: int) -> Path:
        del maximum_bytes
        staging = destination / "staging"
        staging.mkdir()
        (staging / "native.json").write_text("{}", encoding="utf-8")
        return staging

    worker_result = {
        "schema": "quant-runtime.sandbox-worker-result.v1",
        "invocation_id": invocation_id,
        "classification": "success",
        "payload": {"fixture": True},
        "diagnostics": {
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "artifacts": 1,
            "truncated": False,
            "sanitized": True,
        },
    }
    backend.capability_proof = lambda refresh=False: proof
    backend._control = control
    backend.guard_container = lambda _name: None
    backend._wait_for_ready_or_terminal = lambda *_args, **_kwargs: (
        "ready",
        {"Running": True, "Pid": 123, "ExitCode": 0},
    )
    backend._export_output = export
    backend._read_result = lambda _name, *, maximum_bytes: json.dumps(worker_result).encode()
    backend._terminate = lambda _name: None

    result = backend.invoke(prepared)

    assert result["classification"] == "success"
    create = next(call for call in calls if call[0] == "create")
    assert f"{CONTROL_MOUNT}:rw,noexec,nosuid,nodev,size={limits['filesystem_bytes']},mode=1777" in create
    assert f"/sandbox/output:rw,noexec,nosuid,nodev,size={limits['filesystem_bytes']},mode=1777" in create
    assert create[-3:] == ("/sandbox/inputs/invocation.json", CONTROL_RESULT, "/sandbox/output")
