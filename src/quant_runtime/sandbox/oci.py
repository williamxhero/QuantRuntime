from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

import psutil

from quant_runtime.artifacts import read_json, sha256_value, write_json
from quant_runtime.sandbox.invocation import PreparedSandboxInvocation
from quant_runtime.sandbox.outcome import bounded_diagnostics, sandbox_outcome

BACKEND_ID = "docker-engine-linux-oci"
BACKEND_IMPLEMENTATION = "quant-runtime.oci-backend.v1"
MECHANISM = "linux-namespaces-cgroups-seccomp-oci"
SUPPORTED_ENGINE = "29.3.1"
SUPPORTED_CONTAINERD = "v2.2.1"
SUPPORTED_RUNC = "1.3.4"
MECHANISM_VERSION = (
    f"docker-{SUPPORTED_ENGINE}/containerd-{SUPPORTED_CONTAINERD}/runc-{SUPPORTED_RUNC}"
)
PRODUCTION_PROCESS_LIMIT = 127
OCI_PIDS_LIMIT = PRODUCTION_PROCESS_LIMIT + 1


class OciBackendError(RuntimeError):
    """The Docker Engine control plane or capability proof failed closed."""


@dataclass(frozen=True, slots=True)
class OciSandboxConfig:
    image: str
    docker_executable: str = "docker"

    def __post_init__(self) -> None:
        remote_digest = "@sha256:" in self.image and len(self.image.rsplit("@sha256:", 1)[1]) == 64
        local_digest = self.image.startswith("sha256:") and len(self.image) == 71
        if not remote_digest and not local_digest:
            raise ValueError("OCI sandbox image must be pinned by repository digest")

    @property
    def image_digest(self) -> str:
        return (
            self.image
            if self.image.startswith("sha256:")
            else "sha256:" + self.image.rsplit("@sha256:", 1)[1]
        )


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
        if self.config.image.startswith("sha256:"):
            image_matches = image[0].get("Id") == self.config.image
        else:
            image_matches = self.config.image in repo_digests
        if not image_matches:
            raise OciBackendError("OCI dependency image digest does not match the local image")
        labels = image[0].get("Config", {}).get("Labels") or {}
        lock_identity = labels.get("org.quant-runtime.dependency-lock", self.config.image_digest)
        if not isinstance(lock_identity, str) or not _sha256_identity(lock_identity):
            raise OciBackendError("OCI dependency lock identity is invalid")
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
            "dependency_lock_identity": lock_identity,
            "security_options": sorted(str(item) for item in options),
            "controls": {
                "filesystem": "read-only-rootfs+read-only-input-binds+bounded-output-tmpfs",
                "network": "isolated-network-namespace-none",
                "process": f"pids-cgroup-bounded-{OCI_PIDS_LIMIT}",
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
        proof: dict[str, Any] | None = None
        try:
            proof = self.capability_proof()
            profile = _validated_profile(prepared.protocol, proof, self.config.image_digest)
        except (OciBackendError, ValueError) as exc:
            return _result(
                prepared,
                "policy_rejection",
                {"code": "sandbox_capability_unverified", "message": _safe_message(exc)},
                proof=proof,
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
                str(OCI_PIDS_LIMIT),
                "--memory",
                str(limits["memory_bytes"]),
                "--cpus",
                "1",
                "--log-driver",
                "local",
                "--log-opt",
                f"max-size={max(20480, limits['stdout_bytes'] + limits['stderr_bytes'])}",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
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
                f"/sandbox/output:rw,noexec,nosuid,nodev,size={limits['filesystem_bytes']},mode=1777",
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
            self.guard_container(name)
            self._control("start", name, timeout=15)
            status, state = self._wait_for_ready_or_terminal(
                name,
                prepared,
                wall_clock_seconds=limits["wall_clock_seconds"],
            )
            copied_output: Path | None = None
            if status == "ready":
                copied_output = self._export_output(
                    name, prepared.output, maximum_bytes=limits["filesystem_bytes"]
                )
                self._terminate(name)
                state = _json_output(
                    self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
                )
                classification = None
            else:
                classification = status
            logs = self._control("logs", name, timeout=15, check=False)
            stdout_bytes = len(logs.stdout.encode("utf-8"))
            stderr_bytes = len(logs.stderr.encode("utf-8"))
            files = (
                [path for path in copied_output.rglob("*") if path.is_file()]
                if copied_output is not None
                else []
            )
            artifacts = [
                path
                for path in files
                if path.name != "sandbox-result.json" and not path.name.startswith(".")
            ]
            artifact_bytes = sum(path.stat().st_size for path in artifacts)
            terminal = _terminal_proof(proof, state)
            diagnostics = bounded_diagnostics(
                limits=limits,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                artifact_count=len(artifacts),
                artifact_bytes=artifact_bytes,
                artifacts_accepted=min(len(artifacts), limits["artifacts"]),
                terminal_proof=terminal,
            )
            if classification is not None:
                return _result(
                    prepared,
                    classification,
                    {"code": f"sandbox_{classification}"},
                    diagnostics=diagnostics,
                    proof=proof,
                )
            if (
                state.get("OOMKilled") is True
                or (copied_output is None and state.get("ExitCode") in {137, 152})
                or len(artifacts) > limits["artifacts"]
                or artifact_bytes > limits["filesystem_bytes"]
            ):
                return _result(
                    prepared,
                    "resource_exhaustion",
                    {"code": "sandbox_resource_limit_exceeded"},
                    diagnostics=diagnostics,
                    proof=proof,
                )
            if copied_output is None:
                return _result(
                    prepared,
                    "engine_failure",
                    {"code": "sandbox_worker_terminated_without_result"},
                    diagnostics=diagnostics,
                    proof=proof,
                )
            result_path = copied_output / "sandbox-result.json"
            if state.get("ExitCode") != 0 and not result_path.is_file():
                return _result(
                    prepared,
                    "engine_failure",
                    {"code": "sandbox_worker_failed", "exit_code": state.get("ExitCode")},
                    diagnostics=diagnostics,
                    proof=proof,
                )
            worker = read_json(result_path)
            worker_classification = str(worker.get("classification", ""))
            if worker_classification not in {
                "success",
                "policy_rejection",
                "resource_exhaustion",
                "strategy_rejection",
                "engine_failure",
            }:
                raise OciBackendError("sandbox worker returned an invalid classification")
            payload = worker.get("payload")
            if worker.get("invocation_id") != prepared.protocol["invocation_id"] or not isinstance(
                payload, dict
            ):
                raise OciBackendError("sandbox worker result identity is invalid")
            reported = payload.pop("_sandbox_observed", {})
            if isinstance(reported, dict):
                diagnostics = bounded_diagnostics(
                    limits=limits,
                    stdout_bytes=max(stdout_bytes, int(reported.get("stdout_bytes", 0))),
                    stderr_bytes=max(stderr_bytes, int(reported.get("stderr_bytes", 0))),
                    artifact_count=len(artifacts),
                    artifact_bytes=artifact_bytes,
                    artifacts_accepted=min(len(artifacts), limits["artifacts"]),
                    terminal_proof=terminal,
                )
            return _result(
                prepared,
                worker_classification,
                payload,
                diagnostics=diagnostics,
                proof=proof,
            )
        except Exception as exc:
            if created:
                self._terminate(name)
            return _result(
                prepared,
                "engine_failure",
                {"code": "sandbox_backend_failed", "message": _safe_message(exc)},
                proof=proof,
            )
        finally:
            if created:
                self._control("rm", "--force", name, timeout=15, check=False)

    def _wait_for_ready_or_terminal(
        self,
        name: str,
        prepared: PreparedSandboxInvocation,
        *,
        wall_clock_seconds: int,
    ) -> tuple[str | None, dict[str, Any]]:
        started = time.monotonic()
        while True:
            state = _json_output(
                self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
            )
            if state.get("Running") is False:
                return None, state
            if prepared.cancellation.cancelled:
                self._terminate(name)
                state = _json_output(
                    self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
                )
                return "cancellation", state
            if time.monotonic() - started >= wall_clock_seconds:
                self._terminate(name)
                state = _json_output(
                    self._control("inspect", "--format", "{{json .State}}", name, timeout=15)
                )
                return "timeout", state
            ready = self._control(
                "exec",
                name,
                "/usr/local/bin/python",
                "-c",
                (
                    "from pathlib import Path; "
                    "raise SystemExit(not Path('/sandbox/output/.ready').is_file())"
                ),
                timeout=15,
                check=False,
            )
            if ready.returncode == 0:
                return "ready", state
            time.sleep(0.05)

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
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(OCI_PIDS_LIMIT),
            "--user",
            "65534:65534",
            "--entrypoint",
            "/usr/local/bin/python",
            self.config.image,
            "-c",
            (
                "import subprocess; children=[]; blocked=False\n"
                "try:\n"
                " while len(children)<512: children.append(subprocess.Popen(['/bin/sleep','30']))\n"
                "except OSError: blocked=True\n"
                "finally:\n"
                " [child.kill() for child in children]\n"
                " [child.wait() for child in children]\n"
                "print(f'blocked={blocked} spawned={len(children)}')"
            ),
            timeout=30,
            check=False,
        )
        values = dict(item.split("=", 1) for item in process.stdout.strip().split())
        spawned = int(values.get("spawned", "512"))
        if process.returncode != 0 or values.get("blocked") != "True" or not 1 <= spawned < 512:
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
        self._probe_parent_death_guard()
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
            "additional_process_creation_bounded": True,
            "engine_termination_verified": True,
            "parent_death_guard_verified": True,
        }

    def _probe_parent_death_guard(self) -> None:
        name = "quant-runtime-parent-proof-" + uuid.uuid4().hex
        created = False
        parent: subprocess.Popen[bytes] | None = None
        guardian: subprocess.Popen[bytes] | None = None
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
            parent = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(300)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
            )
            guardian = self.guard_container(
                name,
                parent_pid=parent.pid,
                parent_created=psutil.Process(parent.pid).create_time(),
            )
            parent.kill()
            parent.wait(timeout=10)
            guardian.wait(timeout=15)
            inspected = self._control("inspect", name, timeout=15, check=False)
            if inspected.returncode == 0:
                raise OciBackendError("OCI parent-death termination probe failed")
            created = False
        finally:
            if parent is not None and parent.poll() is None:
                parent.kill()
                parent.wait(timeout=10)
            if guardian is not None and guardian.poll() is None:
                guardian.kill()
                guardian.wait(timeout=10)
            if created:
                self._control("rm", "--force", name, timeout=15, check=False)

    def guard_container(
        self,
        name: str,
        *,
        parent_pid: int | None = None,
        parent_created: float | None = None,
    ) -> subprocess.Popen[bytes]:
        pid = os.getpid() if parent_pid is None else parent_pid
        created = psutil.Process(pid).create_time() if parent_created is None else parent_created
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        command = [
            sys.executable,
            "-m",
            "quant_runtime.sandbox.guardian",
            "--docker",
            self._docker,
            "--container",
            name,
            "--parent-pid",
            str(pid),
            "--parent-created",
            repr(created),
        ]
        return subprocess.Popen(command, **kwargs)

    def _export_output(self, name: str, destination: Path, *, maximum_bytes: int) -> Path:
        completed = subprocess.run(
            [
                self._docker,
                "exec",
                "--user",
                "0:0",
                name,
                "/usr/local/bin/python",
                "-c",
                (
                    "import sys,tarfile; "
                    "archive=tarfile.open(fileobj=sys.stdout.buffer,mode='w|'); "
                    "archive.add('/sandbox/output',arcname='.'); archive.close()"
                ),
            ],
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > maximum_bytes + 1_048_576:
            raise OciBackendError("bounded output export failed")
        root = destination / "staging"
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.as_posix() in {".", ""} or member.isdir():
                    continue
                if relative.is_absolute() or ".." in relative.parts or not member.isfile():
                    raise OciBackendError("sandbox output contains an unsafe entry")
                source = archive.extractfile(member)
                if source is None:
                    raise OciBackendError("sandbox output cannot be read")
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
        return root

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
            detail = " ".join(completed.stderr.splitlines())[:256]
            raise OciBackendError(
                f"Docker Engine control operation failed: {arguments[0]}: {detail}"
            )
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
        "implementation": BACKEND_IMPLEMENTATION,
        "mechanism": MECHANISM,
        "mechanism_version": MECHANISM_VERSION,
        "platform": "linux",
        "proof": proof["proof_id"],
    }:
        raise ValueError("sandbox profile does not match the proven OCI mechanism")
    if dependency != {
        "kind": "oci-image",
        "identity": image_digest,
        "lock_identity": proof["dependency_lock_identity"],
    }:
        raise ValueError("sandbox dependency identity does not match the OCI image")
    if capabilities != {"network": "deny", "filesystem": "sealed", "subprocess": "bounded"}:
        raise ValueError("sandbox capabilities are not the production default-deny set")
    if not isinstance(limits, dict) or limits.get("processes") != PRODUCTION_PROCESS_LIMIT:
        raise ValueError(
            "production sandbox process capacity does not match the proven pids cgroup"
        )
    return profile


def _bind(source: Path, target: str) -> str:
    return f"type=bind,source={source.resolve()},target={target},readonly"


def _json_output(completed: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OciBackendError("Docker Engine returned an invalid control response") from exc


def _result(
    prepared: PreparedSandboxInvocation,
    classification: str,
    payload: dict[str, Any],
    *,
    diagnostics: dict[str, Any] | None = None,
    proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = prepared.protocol.get("sandbox_profile", {})
    limits = profile.get("limits", {}) if isinstance(profile, dict) else {}
    if diagnostics is None:
        fallback_limits = {
            "stdout_bytes": int(limits.get("stdout_bytes", 0)),
            "stderr_bytes": int(limits.get("stderr_bytes", 0)),
            "artifacts": int(limits.get("artifacts", 0)),
        }
        diagnostics = bounded_diagnostics(
            limits=fallback_limits,
            stdout_bytes=0,
            stderr_bytes=0,
            artifact_count=0,
            artifact_bytes=0,
            artifacts_accepted=0,
            terminal_proof=_terminal_proof(proof, None),
        )
    outcome = sandbox_outcome(classification, diagnostics=diagnostics, payload=payload)
    return {
        "schema": "quant-runtime.sandbox-worker-result.v2",
        "invocation_id": prepared.protocol["invocation_id"],
        "classification": classification,
        "payload": payload,
        "sandbox": outcome,
    }


def _terminal_proof(proof: dict[str, Any] | None, state: dict[str, Any] | None) -> dict[str, Any]:
    if state is not None and (state.get("Running") is not False or int(state.get("Pid", 0)) != 0):
        raise OciBackendError("sandbox terminal state is not proven")
    return {
        "backend_id": BACKEND_ID,
        "mechanism_version": MECHANISM_VERSION,
        "proof_id": proof["proof_id"] if proof is not None else "sha256:" + "0" * 64,
        "candidate_terminated": True,
        "descendants_terminated": True,
        "running_processes": 0,
    }


def _safe_message(error: Exception) -> str:
    del error
    return "sandbox operation failed; inspect bounded internal telemetry"


def _sha256_identity(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == 71
        and all(item in "0123456789abcdef" for item in value[7:])
    )
