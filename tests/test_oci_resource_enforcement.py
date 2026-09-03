from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant_runtime.sandbox import CancellationToken
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation
from quant_runtime.sandbox.oci import (
    BACKEND_ID,
    MECHANISM,
    MECHANISM_VERSION,
    OciSandboxBackend,
    OciSandboxConfig,
)

IMAGE = "sha256:15c448b12dc408ebb72972818bd0b598e4a2cc51a5740d3c530c059218c1f354"


def _image_ready() -> bool:
    executable = shutil.which("docker")
    if executable is None:
        return False
    return (
        subprocess.run(
            [executable, "image", "inspect", IMAGE],
            check=False,
            capture_output=True,
            timeout=15,
            shell=False,
        ).returncode
        == 0
    )


pytestmark = [
    pytest.mark.oci,
    pytest.mark.skipif(not _image_ready(), reason="exact Runtime probe image is unavailable"),
]


@pytest.fixture(scope="module")
def backend_and_proof() -> tuple[OciSandboxBackend, dict]:
    backend = OciSandboxBackend(OciSandboxConfig(image=IMAGE))
    return backend, backend.capability_proof(refresh=True)


def _prepared(
    tmp_path: Path,
    proof: dict,
    *,
    mode: str,
    limits: dict[str, int] | None = None,
    token: CancellationToken | None = None,
    probe: dict | None = None,
) -> PreparedSandboxInvocation:
    package = tmp_path / "package"
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    package.mkdir()
    inputs.mkdir()
    output.mkdir()
    (inputs / "probe.json").write_text(
        json.dumps({"mode": mode, **(probe or {})}), encoding="utf-8"
    )
    budget = {
        "cpu_seconds": 2,
        "memory_bytes": 67_108_864,
        "wall_clock_seconds": 10,
        "processes": 1,
        "filesystem_bytes": 1_048_576,
        "stdout_bytes": 20_480,
        "stderr_bytes": 20_480,
        "artifacts": 4,
        **(limits or {}),
    }
    profile = {
        "schema": "quant-runtime.sandbox-profile.v1",
        "profile_id": "oci-probe",
        "revision": 1,
        "execution_mode": "isolated",
        "trust_classification": "generated_untrusted",
        "containment": {
            "backend_id": BACKEND_ID,
            "mechanism": MECHANISM,
            "mechanism_version": MECHANISM_VERSION,
            "platform": "linux",
            "proof": proof["proof_id"],
        },
        "dependency_environment": {"kind": "oci-image", "identity": IMAGE},
        "capabilities": {"network": "deny", "filesystem": "sealed", "subprocess": "deny"},
        "limits": budget,
    }
    return PreparedSandboxInvocation(
        {
            "schema": "quant-runtime.sandbox-invocation.v1",
            "invocation_id": "sha256:" + "a" * 64,
            "sandbox_profile": profile,
            "phase": "sandbox_probe",
        },
        SimpleNamespace(root=package),
        inputs,
        output,
        token or CancellationToken(),
    )


def test_process_storm_is_blocked_and_terminal_proof_is_present(
    tmp_path: Path, backend_and_proof
) -> None:
    backend, proof = backend_and_proof
    result = backend.invoke(_prepared(tmp_path, proof, mode="process"))

    assert result["classification"] == "success"
    assert result["payload"]["spawn_blocked"] is True
    assert result["sandbox"]["diagnostics"]["terminal_proof"]["descendants_terminated"] is True


def test_wall_clock_timeout_kills_before_return(tmp_path: Path, backend_and_proof) -> None:
    backend, proof = backend_and_proof
    result = backend.invoke(
        _prepared(tmp_path, proof, mode="sleep", limits={"wall_clock_seconds": 1})
    )

    assert result["classification"] == "timeout"
    assert result["sandbox"]["retryable"] is True
    assert result["sandbox"]["diagnostics"]["terminal_proof"]["running_processes"] == 0


def test_caller_cancellation_kills_before_return(tmp_path: Path, backend_and_proof) -> None:
    backend, proof = backend_and_proof
    token = CancellationToken()
    timer = threading.Timer(0.5, token.cancel)
    timer.start()
    try:
        result = backend.invoke(_prepared(tmp_path, proof, mode="sleep", token=token))
    finally:
        timer.cancel()

    assert result["classification"] == "cancellation"
    assert result["sandbox"]["diagnostics"]["terminal_proof"]["candidate_terminated"] is True


@pytest.mark.parametrize(
    ("mode", "limits"),
    [
        ("cpu", {"cpu_seconds": 1}),
        ("memory", {"memory_bytes": 33_554_432}),
        ("artifacts", {"artifacts": 2}),
        ("filesystem", {"filesystem_bytes": 1_048_576}),
    ],
)
def test_cpu_memory_and_artifact_floods_are_resource_exhaustion(
    tmp_path: Path, backend_and_proof, mode: str, limits: dict[str, int]
) -> None:
    backend, proof = backend_and_proof
    result = backend.invoke(_prepared(tmp_path, proof, mode=mode, limits=limits))

    assert result["classification"] == "resource_exhaustion"
    assert result["sandbox"]["retryable"] is False


def test_output_is_counted_truncated_and_never_returned(tmp_path: Path, backend_and_proof) -> None:
    backend, proof = backend_and_proof
    result = backend.invoke(_prepared(tmp_path, proof, mode="output", probe={"bytes": 262_144}))

    assert result["classification"] == "success"
    diagnostics = result["sandbox"]["diagnostics"]
    assert diagnostics["stdout"]["observed_bytes"] > diagnostics["stdout"]["limit_bytes"]
    assert diagnostics["stdout"]["retained_bytes"] == diagnostics["stdout"]["limit_bytes"]
    assert diagnostics["stdout"]["truncated"] is True
    assert "x" * 100 not in json.dumps(result)


@pytest.mark.parametrize("mode", ["malformed", "secret"])
def test_malformed_or_sensitive_worker_output_is_bounded_and_sanitized(
    tmp_path: Path, backend_and_proof, mode: str
) -> None:
    backend, proof = backend_and_proof
    result = backend.invoke(_prepared(tmp_path, proof, mode=mode))

    if mode == "malformed":
        assert result["classification"] == "engine_failure"
    else:
        assert result["classification"] == "success"
    encoded = json.dumps(result)
    assert "must-not-cross" not in encoded
    assert "C:\\\\Users" not in encoded
    assert "/home/private" not in encoded
    assert "token=secret" not in encoded
    assert result["sandbox"]["diagnostics"]["sanitized"] is True
