from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PACKAGE
from strategy_workspace import WorkspaceClient
from test_sandbox_policy import isolated_profile

from quant_runtime.artifacts import sha256_value
from quant_runtime.sandbox import CancellationToken, SandboxRunner
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
        "process": "pids-cgroup-one-candidate-plus-runtime-supervisor",
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
        CancellationToken(),
    )

    result = backend.invoke(prepared)

    assert result["classification"] == "policy_rejection"
    assert result["payload"]["code"] == "sandbox_capability_unverified"
    assert not (inputs / "invocation.json").exists()


@pytest.mark.oci
@pytest.mark.skipif(not _docker_ready(), reason="pinned OCI proof image is unavailable")
def test_parent_death_guard_kills_and_removes_the_container() -> None:
    executable = shutil.which("docker")
    assert executable is not None
    name = "quant-runtime-parent-proof-" + uuid.uuid4().hex
    common = [executable]
    subprocess.run(
        [
            *common,
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "1",
            "--user",
            "65534:65534",
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            "-c",
            "while :; do :; done",
        ],
        check=True,
        capture_output=True,
        shell=False,
    )
    try:
        subprocess.run([*common, "start", name], check=True, capture_output=True, shell=False)
        code = (
            "import time\n"
            "from quant_runtime.sandbox.oci import OciSandboxBackend,OciSandboxConfig\n"
            f"backend=OciSandboxBackend(OciSandboxConfig(image={IMAGE!r}))\n"
            f"backend.guard_container({name!r})\n"
            "print('ready', flush=True)\n"
            "time.sleep(300)\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        assert parent.stdout is not None
        assert parent.stdout.readline().strip() == "ready"
        parent.kill()
        parent.wait(timeout=10)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            inspected = subprocess.run(
                [*common, "inspect", name], check=False, capture_output=True, shell=False
            )
            if inspected.returncode != 0:
                break
            time.sleep(0.1)
        assert inspected.returncode != 0
    finally:
        subprocess.run(
            [*common, "rm", "--force", name], check=False, capture_output=True, shell=False
        )


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
    scenario = client.publish_record(
        {"record_id": "scenario", "record_type": "fixture", "payload": {}},
        artifacts=({"source": b"{}", "logical_role": "scenario", "name": "scenario.json"},),
    )["artifacts"][0]

    profile = isolated_profile()
    profile["trust_classification"] = "human_isolated"
    result = SandboxRunner(client, backend=UnsupportedSandboxBackend()).invoke(
        package_record=registered,
        profile=profile,
        phase="behavioral_conformance",
        parameters={},
        input_artifacts={"scenario.json": scenario},
    )

    assert result["classification"] == "policy_rejection"
    assert result["payload"]["code"] == "sandbox_platform_unsupported"
    assert result["sandbox"]["terminal_status"] == "failed"
    assert result["sandbox"]["diagnostics"]["terminal_proof"]["running_processes"] == 0
    assert not marker.exists()
