from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from quant_runtime.artifacts import read_json, sha256_value, write_json
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation

BACKEND_ID = "docker-engine-linux-oci"
BACKEND_IMPLEMENTATION = "quant-runtime.oci-backend.v1"
MECHANISM = "linux-namespaces-cgroups-seccomp-oci"
SUPPORTED_ENGINE = "29.3.1"
SUPPORTED_CONTAINERD = "v2.2.1"
SUPPORTED_RUNC = "1.3.4"
MECHANISM_VERSION = (
    f"docker-{SUPPORTED_ENGINE}/containerd-{SUPPORTED_CONTAINERD}/runc-{SUPPORTED_RUNC}"
)


class OciBackendError(RuntimeError):
    """The Docker Engine control plane or capability proof failed closed."""


@dataclass(frozen=True, slots=True)
class OciSandboxConfig:
    image: str
    docker_executable: str = "docker"

    def __post_init__(self) -> None:
        if "@sha256:" not in self.image or len(self.image.rsplit("@sha256:", 1)[1]) != 64:
            raise ValueError("OCI sandbox image must be pinned by repository digest")

    @property
    def image_digest(self) -> str:
        return "sha256:" + self.image.rsplit("@sha256:", 1)[1]


class OciSandboxBackend:
    """Production containment through a proven Linux Docker Engine/runc boundary.

    The Docker CLI is only an argument-vector transport to the Engine control API. The
    security boundary is the named Linux namespace/cgroup/seccomp OCI mechanism attested
    by ``capability_proof``; a local child process is never treated as containment.
    """

    production = True

    def __init__(self, config: OciSandboxConfig) -> None:
        self.config = config
        executable = shutil.which(config.docker_executable)
        if executable is None:
            raise OciBackendError("Docker Engine control client is unavailable")
        self._docker = executable
        self._proof: dict[str, Any] | None = None

    def capability_proof(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._proof is not None and not refresh:
            return json.loads(json.dumps(self._proof))
        server = _json_output(self._control("version", "--format", "{{json .Server}}", timeout=15))
        components = {
            str(item["Name"]): str(item["Version"])
            for item in server.get("Components", [])
            if isinstance(item, dict) and "Name" in item and "Version" in item
        }
        if (
            server.get("Os") != "linux"
            or server.get("Version") != SUPPORTED_ENGINE
            or components.get("containerd") != SUPPORTED_CONTAINERD
            or components.get("runc") != SUPPORTED_RUNC
        ):
            raise OciBackendError("Docker Engine containment version is unsupported")
        options = _json_output(
            self._control("info", "--format", "{{json .SecurityOptions}}", timeout=15)
        )
        if not isinstance(options, list) or not {
            "name=seccomp,profile=builtin",
            "name=cgroupns",
        } <= set(options):
            raise OciBackendError("Docker Engine lacks required seccomp or cgroup namespaces")
        image = _json_output(self._control("image", "inspect", self.config.image, timeout=15))
        if not isinstance(image, list) or len(image) != 1:
            raise OciBackendError("OCI dependency image identity is unavailable")
        repo_digests = image[0].get("RepoDigests", [])
        if self.config.image not in repo_digests:
            raise OciBackendError("OCI dependency image digest does not match the local image")
        probes = self._adversarial_probes()
        statement = {
            "schema": "quant-runtime.oci-capability-proof.v1",
            "backend_id": BACKEND_ID,
            "backend_implementation": BACKEND_IMPLEMENTATION,
            "mechanism": MECHANISM,
            "mechanism_version": MECHANISM_VERSION,
            "platform": "linux",
            "engine": {
                "docker": SUPPORTED_ENGINE,
                "containerd": SUPPORTED_CONTAINERD,
                "runc": SUPPORTED_RUNC,
                "kernel": str(server.get("KernelVersion", "")),
            },
            "dependency_image": self.config.image,
            "dependency_identity": self.config.image_digest,
            "security_options": sorted(str(item) for item in options),
            "controls": {
                "filesystem": "read-only-rootfs+read-only-input-binds+bounded-output-tmpfs",
                "network": "isolated-network-namespace-none",
                "process": "pids-cgroup-one-process",
                "privilege": "uid-65534+cap-drop-all+no-new-privileges+seccomp",
                "environment": "no-host-environment-forwarding",
                "termination": "engine-kill+stopped-state-verification",
            },
            "probes": probes,
        }
        proof = {**statement, "proof_id": "sha256:" + sha256_value(statement)}
        self._proof = proof
        return json.loads(json.dumps(proof))

    def invoke(self, prepared: PreparedSandboxInvocation) -> dict[str, Any]:
        try:
            proof = self.capability_proof()
            profile = _validated_profile(prepared.protocol, proof, self.config.image_digest)
        except (OciBackendError, ValueError) as exc:
            return _result(
                prepared,
                "policy_rejection",
                {"code": "sandbox_capability_unverified", "message": _safe_message(exc)},
            )
        write_json(prepared.inputs / "invocation.json", prepared.protocol)
        name = "quant-runtime-" + uuid.uuid4().hex
        limits = profile["limits"]
        created = False
        try:
            self._control(
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
                "--memory",
                str(limits["memory_bytes"]),
                "--cpus",
                "1",
                "--ulimit",
                f"cpu={limits['cpu_seconds']}:{limits['cpu_seconds']}",
                "--user",
                "65534:65534",
                "--env",
                "HOME=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONHASHSEED=0",
                "--env",
                "OMP_NUM_THREADS=1",
                "--env",
                "OPENBLAS_NUM_THREADS=1",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16777216",
                "--tmpfs",
                f"/sandbox/output:rw,noexec,nosuid,nodev,size={limits['filesystem_bytes']}",
                "--mount",
                _bind(prepared.package.root, "/sandbox/package"),
                "--mount",
                _bind(prepared.inputs, "/sandbox/inputs"),
                "--entrypoint",
                "/usr/local/bin/python",
                self.config.image,
                "-m",
                "quant_runtime.sandbox.worker",
                "/sandbox/inputs/invocation.json",
                "/sandbox/output/sandbox-result.json",
                timeout=30,
            )
            created = True
            started = self._control(
                "start",
                "--attach",
                name,
                timeout=max(1, limits["wall_clock_seconds"]),
                check=False,
            )
            state = _json_output(
                self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
            )
            if state.get("Running") is not False:
                raise OciBackendError("sandbox candidate remained running after execution")
            self._control("cp", f"{name}:/sandbox/output/.", str(prepared.output), timeout=30)
            result_path = prepared.output / "sandbox-result.json"
            if started.returncode != 0 and not result_path.is_file():
                return _result(
                    prepared,
                    "engine_failure",
                    {"code": "sandbox_worker_failed", "exit_code": state.get("ExitCode")},
                )
            return read_json(result_path)
        except subprocess.TimeoutExpired:
            if created:
                self._terminate(name)
            return _result(prepared, "timeout", {"code": "sandbox_wall_clock_exceeded"})
        except Exception as exc:
            if created:
                self._terminate(name)
            return _result(
                prepared,
                "engine_failure",
                {"code": "sandbox_backend_failed", "message": _safe_message(exc)},
            )
        finally:
            if created:
                self._control("rm", "--force", name, timeout=15, check=False)

    def _adversarial_probes(self) -> dict[str, bool]:
        common = (
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "67108864",
            "--cpus",
            "0.5",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=1048576",
            "--entrypoint",
            "/bin/sh",
        )
        with TemporaryDirectory(prefix="quant-runtime-proof-") as temporary:
            root = Path(temporary)
            package = root / "package"
            inputs = root / "inputs"
            package.mkdir()
            inputs.mkdir()
            (package / "sealed").write_text("package", encoding="utf-8")
            (inputs / "sealed").write_text("input", encoding="utf-8")
            script = (
                'uid=$(id -u); nets=$(ls /sys/class/net | tr "\\n" ","); '
                'caps=$(sed -n "s/^CapEff:[[:space:]]*//p" /proc/self/status); '
                'nnp=$(sed -n "s/^NoNewPrivs:[[:space:]]*//p" /proc/self/status); '
                "touch /escape 2>/dev/null && rootwrite=bad || rootwrite=blocked; "
                "touch /sandbox/package/new 2>/dev/null "
                "&& packagewrite=bad || packagewrite=blocked; "
                "touch /sandbox/inputs/new 2>/dev/null && inputwrite=bad || inputwrite=blocked; "
                "touch /sandbox/output/allowed 2>/dev/null "
                "&& outputwrite=allowed || outputwrite=bad; "
                "dd if=/dev/zero of=/sandbox/output/oversize bs=8192 count=1 2>/dev/null "
                "&& outputlimit=bad || outputlimit=bounded; "
                "test -e /run/desktop/mnt/host/c/Users && hostfs=exposed || hostfs=hidden; "
                'test -n "$QUANT_RUNTIME_HOST_SECRET" && envleak=bad || envleak=blocked; '
                'printf "uid=%s nets=%s caps=%s nnp=%s root=%s package=%s input=%s '
                'output=%s limit=%s hostfs=%s env=%s" "$uid" "$nets" "$caps" "$nnp" '
                '"$rootwrite" "$packagewrite" "$inputwrite" "$outputwrite" "$outputlimit" '
                '"$hostfs" "$envleak"'
            )
            security = self._control(
                *common,
                "--pids-limit",
                "16",
                "--mount",
                _bind(package, "/sandbox/package"),
                "--mount",
                _bind(inputs, "/sandbox/inputs"),
                "--tmpfs",
                "/sandbox/output:rw,noexec,nosuid,nodev,size=4096",
                self.config.image,
                "-c",
                script,
                timeout=30,
            ).stdout
        expected = (
            "uid=65534 nets=lo, caps=0000000000000000 nnp=1 root=blocked "
            "package=blocked input=blocked output=allowed limit=bounded hostfs=hidden env=blocked"
        )
        if security.strip() != expected:
            raise OciBackendError("OCI filesystem/network/environment/privilege probe failed")
        process = self._control(
            *common,
            "--pids-limit",
            "1",
            self.config.image,
            "-c",
            "/bin/true & child=$!; wait $child",
            timeout=30,
            check=False,
        )
        if process.returncode == 0 or "can't fork" not in (process.stdout + process.stderr):
            raise OciBackendError("OCI process-boundary probe failed")
        name = "quant-runtime-proof-" + uuid.uuid4().hex
        created = False
        try:
            self._control(
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
                self.config.image,
                "-c",
                "while :; do :; done",
                timeout=30,
            )
            created = True
            self._control("start", name, timeout=15)
            self._terminate(name)
            state = _json_output(
                self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
            )
            if state.get("Running") is not False:
                raise OciBackendError("OCI termination probe failed")
        finally:
            if created:
                self._control("rm", "--force", name, timeout=15, check=False)
        return {
            "root_filesystem_write_blocked": True,
            "package_bind_read_only": True,
            "input_bind_read_only": True,
            "bounded_output_tmpfs_writable": True,
            "host_private_files_inaccessible": True,
            "host_environment_not_forwarded": True,
            "network_namespace_has_loopback_only": True,
            "effective_capabilities_empty": True,
            "no_new_privileges_enabled": True,
            "additional_process_creation_blocked": True,
            "engine_termination_verified": True,
        }

    def _terminate(self, name: str) -> None:
        self._control("kill", name, timeout=15, check=False)
        state = _json_output(
            self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
        )
        if state.get("Running") is not False:
            raise OciBackendError("sandbox termination could not be verified")

    def _control(
        self,
        *arguments: str,
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [self._docker, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env={**os.environ, "QUANT_RUNTIME_HOST_SECRET": "must-not-cross-oci-boundary"},
        )
        if check and completed.returncode != 0:
            raise OciBackendError("Docker Engine control operation failed")
        return completed


def production_backend() -> OciSandboxBackend | Any:
    image = os.environ.get("QUANT_RUNTIME_OCI_IMAGE", "")
    if image:
        return OciSandboxBackend(OciSandboxConfig(image=image))
    from quant_runtime.sandbox.backend import UnsupportedSandboxBackend

    return UnsupportedSandboxBackend()


def _validated_profile(
    protocol: dict[str, Any], proof: dict[str, Any], image_digest: str
) -> dict[str, Any]:
    profile = protocol.get("sandbox_profile")
    if not isinstance(profile, dict):
        raise ValueError("sandbox protocol lacks its versioned profile")
    containment = profile.get("containment")
    dependency = profile.get("dependency_environment")
    capabilities = profile.get("capabilities")
    limits = profile.get("limits")
    if containment != {
        "backend_id": BACKEND_ID,
        "mechanism": MECHANISM,
        "mechanism_version": MECHANISM_VERSION,
        "platform": "linux",
        "proof": proof["proof_id"],
    }:
        raise ValueError("sandbox profile does not match the proven OCI mechanism")
    if dependency != {"kind": "oci-image", "identity": image_digest}:
        raise ValueError("sandbox dependency identity does not match the OCI image")
    if capabilities != {"network": "deny", "filesystem": "sealed", "subprocess": "deny"}:
        raise ValueError("sandbox capabilities are not the production default-deny set")
    if not isinstance(limits, dict) or limits.get("processes") != 1:
        raise ValueError("production sandbox requires a one-process pids cgroup")
    return profile


def _bind(source: Path, target: str) -> str:
    return f"type=bind,source={source.resolve()},target={target},readonly"


def _json_output(completed: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OciBackendError("Docker Engine returned an invalid control response") from exc


def _result(
    prepared: PreparedSandboxInvocation, classification: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": "quant-runtime.sandbox-worker-result.v1",
        "invocation_id": prepared.protocol["invocation_id"],
        "classification": classification,
        "payload": payload,
        "diagnostics": {
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "artifacts": 0,
            "truncated": False,
            "sanitized": True,
        },
    }


def _safe_message(error: Exception) -> str:
    return type(error).__name__ + ": " + str(error).splitlines()[0][:256]
