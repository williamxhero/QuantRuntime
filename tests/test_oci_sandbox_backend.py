from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PACKAGE
from strategy_workspace import WorkspaceClient
from test_sandbox_policy import isolated_profile

from quant_runtime.artifacts import sha256_value
from quant_runtime.sandbox import SandboxRunner
from quant_runtime.sandbox.backend import UnsupportedSandboxBackend
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation
from quant_runtime.sandbox.oci import (
    BACKEND_ID,
    BACKEND_IMPLEMENTATION,
    MECHANISM,
    MECHANISM_VERSION,
    OciSandboxBackend,
    OciSandboxConfig,
)

IMAGE = "valkey/valkey@sha256:e1095c6c76ee982cb2d1e07edbb7fb2a53606630a1d810d5a47c9f646b708bf5"


def _docker_ready() -> bool:
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


@pytest.mark.oci
@pytest.mark.skipif(not _docker_ready(), reason="pinned OCI proof image is unavailable")
def test_public_backend_proof_attests_real_kernel_boundaries_and_termination() -> None:
    backend = OciSandboxBackend(OciSandboxConfig(image=IMAGE))
    proof = backend.capability_proof(refresh=True)

    assert proof["backend_id"] == BACKEND_ID
    assert proof["backend_implementation"] == BACKEND_IMPLEMENTATION
    assert proof["mechanism"] == MECHANISM
    assert proof["mechanism_version"] == MECHANISM_VERSION
    assert proof["platform"] == "linux"
    assert proof["dependency_image"] == IMAGE
    assert set(proof["probes"].values()) == {True}
    statement = {key: value for key, value in proof.items() if key != "proof_id"}
    assert proof["proof_id"] == "sha256:" + sha256_value(statement)
    assert proof["controls"] == {
        "filesystem": "read-only-rootfs+read-only-input-binds+bounded-output-tmpfs",
        "network": "isolated-network-namespace-none",
        "process": "pids-cgroup-one-process",
        "privilege": "uid-65534+cap-drop-all+no-new-privileges+seccomp",
        "environment": "no-host-environment-forwarding",
        "termination": "engine-kill+stopped-state-verification",
    }


@pytest.mark.oci
@pytest.mark.skipif(not _docker_ready(), reason="pinned OCI proof image is unavailable")
def test_exact_image_proof_cannot_authorize_a_different_worker_image(tmp_path: Path) -> None:
    backend = OciSandboxBackend(OciSandboxConfig(image=IMAGE))
    proof = backend.capability_proof()
    profile = isolated_profile()
    profile["containment"] = {
        "backend_id": BACKEND_ID,
        "mechanism": MECHANISM,
        "mechanism_version": MECHANISM_VERSION,
        "platform": "linux",
        "proof": proof["proof_id"],
    }
    profile["dependency_environment"] = {"kind": "oci-image", "identity": "sha256:" + "d" * 64}
    profile["limits"]["processes"] = 1
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    package = tmp_path / "package"
    inputs.mkdir()
    output.mkdir()
    package.mkdir()
    prepared = PreparedSandboxInvocation(
        {
            "invocation_id": "sha256:" + "a" * 64,
            "sandbox_profile": profile,
        },
        SimpleNamespace(root=package),
        inputs,
        output,
    )

    result = backend.invoke(prepared)

    assert result["classification"] == "policy_rejection"
    assert result["payload"]["code"] == "sandbox_capability_unverified"
    assert not (inputs / "invocation.json").exists()


def test_unsupported_backend_never_imports_candidate_code(tmp_path: Path) -> None:
    package = tmp_path / "generated"
    shutil.copytree(PACKAGE, package)
    marker = tmp_path / "candidate-imported"
    strategy = package / "strategy.py"
    strategy.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('launched')\n"
        + strategy.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    client = WorkspaceClient(tmp_path / "workspace")
    registered = client.register_package(package)
    registered["manifest"]["schema"] = "quant-research.strategy-package.v2"
    registered["declared_content"] = [
        {
            "path": path.relative_to(package).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.name != "strategy.toml"
    ]
    scenario = client.publish_record(
        {"record_id": "scenario", "record_type": "fixture", "payload": {}},
        artifacts=({"source": b"{}", "logical_role": "scenario", "name": "scenario.json"},),
    )["artifacts"][0]

    result = SandboxRunner(client, backend=UnsupportedSandboxBackend()).invoke(
        package_record=registered,
        profile=isolated_profile(),
        phase="behavioral_conformance",
        parameters={},
        input_artifacts={"scenario.json": scenario},
    )

    assert result["classification"] == "policy_rejection"
    assert result["payload"]["code"] == "sandbox_platform_unsupported"
    assert not marker.exists()
