from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--candidate":
        return _candidate(arguments[1:])
    if len(arguments) != 2:
        return 2
    result_path = Path(arguments[1])
    candidate = subprocess.Popen(
        [sys.executable, "-m", "quant_runtime.sandbox.worker", "--candidate", *arguments],
        shell=False,
    )
    return_code = candidate.wait()
    if not result_path.is_file():
        classification = "resource_exhaustion" if return_code < 0 else "engine_failure"
        _write(
            result_path,
            _result(
                _read(Path(arguments[0])),
                classification,
                {"code": "candidate_terminated", "return_code": return_code},
            ),
        )
    (result_path.parent / ".ready").write_bytes(b"")
    while True:
        time.sleep(60)


def _candidate(arguments: list[str]) -> int:
    if len(arguments) != 2:
        return 2
    protocol_path = Path(arguments[0])
    result_path = Path(arguments[1])
    protocol = _read(protocol_path)
    if protocol.get("phase") == "behavioral_conformance":
        return _conformance(protocol, result_path)
    if protocol.get("phase") == "discovery":
        return _discovery(protocol, result_path)
    if protocol.get("phase") == "formal":
        return _formal(protocol, result_path)
    if protocol.get("phase") != "sandbox_probe":
        _write(result_path, _result(protocol, "policy_rejection", {"code": "worker_phase_invalid"}))
        return 0
    probe = _read(Path("/sandbox/inputs/probe.json"))
    mode = probe.get("mode")
    if mode == "sleep":
        time.sleep(300)
    elif mode == "cpu":
        while True:
            pass
    elif mode == "memory":
        values: list[bytes] = []
        while True:
            values.append(b"x" * 1_048_576)
    elif mode == "output":
        output_bytes = int(probe.get("bytes", 262_144))
        os.write(1, b"x" * output_bytes)
    elif mode == "artifacts":
        for index in range(int(probe.get("count", 100))):
            Path(f"/sandbox/output/artifact-{index:04d}").write_bytes(b"x")
    elif mode == "filesystem":
        flood = Path("/sandbox/output/filesystem-flood")
        try:
            with flood.open("wb") as stream:
                while True:
                    stream.write(b"x" * 65_536)
        except OSError:
            flood.unlink(missing_ok=True)
            _write(
                result_path,
                _result(
                    protocol,
                    "resource_exhaustion",
                    {"code": "sandbox_filesystem_limit_exceeded"},
                ),
            )
            return 0
    elif mode == "process":
        processes: list[subprocess.Popen[bytes]] = []
        blocked = False
        try:
            while len(processes) < 512:
                processes.append(subprocess.Popen(["/bin/sleep", "300"], shell=False))
        except OSError:
            blocked = True
        finally:
            for process in processes:
                process.kill()
            for process in processes:
                process.wait()
        _write(
            result_path,
            _result(
                protocol,
                "success",
                {"spawn_blocked": blocked, "spawned_processes": len(processes)},
            ),
        )
        return 0
    elif mode == "malformed":
        result_path.write_text("{", encoding="utf-8")
        return 0
    elif mode == "secret":
        os.write(
            2,
            (
                str(os.environ.get("QUANT_RUNTIME_HOST_SECRET", "absent"))
                + " C:\\Users\\private /home/private token=secret"
            ).encode(),
        )
    else:
        _write(result_path, _result(protocol, "policy_rejection", {"code": "probe_invalid"}))
        return 0
    payload = {"mode": mode}
    if mode == "output":
        payload["_sandbox_observed"] = {"stdout_bytes": output_bytes, "stderr_bytes": 0}
    _write(result_path, _result(protocol, "success", payload))
    return 0


_CONFORMANCE_DIMENSIONS = frozenset(
    {
        "decision_time",
        "warm_up",
        "strict_comparison",
        "entry",
        "exit",
        "sizing",
        "state_transition",
        "add_reduce",
    }
)


def _conformance(protocol: dict[str, Any], result_path: Path) -> int:
    from quant_runtime.artifacts import sha256_value
    from quant_runtime.entrypoint import load_package_entrypoint

    config = protocol.get("phase_config")
    package_manifest = protocol.get("package_manifest")
    if (
        not isinstance(config, dict)
        or config.get("adapter") != "runtime"
        or not isinstance(config.get("entrypoint"), str)
        or not isinstance(package_manifest, dict)
        or _manifest_conformance_entrypoint(package_manifest) != config.get("entrypoint")
    ):
        _write(
            result_path,
            _result(protocol, "policy_rejection", {"code": "conformance_protocol_invalid"}),
        )
        return 0
    try:
        parameters = _read(Path("/sandbox/inputs/parameters.json"))
        if sha256_value(parameters) != protocol.get("parameters_hash"):
            raise ValueError("sandbox parameters identity differs")
        scenarios = _conformance_scenarios(protocol)
    except Exception:
        _write(
            result_path,
            _result(protocol, "policy_rejection", {"code": "conformance_input_invalid"}),
        )
        return 0
    try:
        conform = load_package_entrypoint(Path("/sandbox/package"), str(config["entrypoint"]))
        if not callable(conform):
            raise TypeError("package conformance entrypoint is not callable")
        traces: list[dict[str, Any]] = []
        dimensions = {
            dimension: {"status": "passed", "observed": "frozen-fixture-exact-match"}
            for dimension in sorted(_CONFORMANCE_DIMENSIONS)
        }
        encoded_bytes = 0
        rejected = False
        for index, scenario in enumerate(scenarios):
            observed = conform(dict(scenario["input"]), dict(parameters))
            if not isinstance(observed, dict):
                raise TypeError("package conformance result must be an object")
            encoded = json.dumps(
                observed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            encoded_bytes += len(encoded)
            if encoded_bytes > 65_536:
                raise ValueError("package conformance output is too large")
            passed = observed == scenario["expected"]
            dimension = str(scenario["dimension"])
            if not passed:
                dimensions[dimension] = {
                    "status": "rejected",
                    "observed": "frozen-fixture-mismatch",
                }
                rejected = True
            traces.append(
                {
                    "event": index,
                    "scenario": scenario["scenario_id"],
                    "dimension": dimension,
                    "status": "passed" if passed else "rejected",
                    "expected_hash": sha256_value(scenario["expected"]),
                    "observed_hash": sha256_value(observed),
                }
            )
    except Exception:
        _write(
            result_path,
            _result(protocol, "strategy_rejection", {"code": "conformance_strategy_rejected"}),
        )
        return 0
    _write(
        result_path,
        _result(
            protocol,
            "success",
            {
                "schema": "quant-runtime.behavioral-conformance.v1",
                "status": "rejected" if rejected else "passed",
                "dimensions": dimensions,
                "trace": traces,
            },
        ),
    )
    return 0


def _manifest_conformance_entrypoint(manifest: dict[str, Any]) -> str | None:
    implementations = manifest.get("implementations")
    if not isinstance(implementations, dict):
        return None
    conformance = implementations.get("conformance")
    if not isinstance(conformance, dict) or set(conformance) != {"runtime"}:
        return None
    entrypoint = conformance.get("runtime")
    return entrypoint if isinstance(entrypoint, str) and entrypoint else None


def _conformance_scenarios(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    refs = protocol.get("input_refs")
    if not isinstance(refs, dict) or "parameters.json" not in refs:
        raise ValueError("conformance input references are incomplete")
    names = sorted(name for name in refs if name != "parameters.json")
    if (
        not names
        or len(names) > 256
        or any(not re.fullmatch(r"scenario-[0-9]{4}\.json", name) for name in names)
    ):
        raise ValueError("conformance scenario names are invalid")
    scenarios: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    dimensions: set[str] = set()
    for name in names:
        scenario = _read(Path("/sandbox/inputs") / name)
        if (
            set(scenario) != {"schema", "scenario_id", "dimension", "input", "expected"}
            or scenario.get("schema") != "quant-runtime.behavioral-scenario.v1"
            or not isinstance(scenario.get("scenario_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", scenario["scenario_id"])
            or scenario.get("dimension") not in _CONFORMANCE_DIMENSIONS
            or not isinstance(scenario.get("input"), dict)
            or not isinstance(scenario.get("expected"), dict)
            or scenario["scenario_id"] in identifiers
        ):
            raise ValueError("conformance scenario is invalid")
        identifiers.add(scenario["scenario_id"])
        dimensions.add(str(scenario["dimension"]))
        scenarios.append(scenario)
    if dimensions != _CONFORMANCE_DIMENSIONS:
        raise ValueError("conformance scenarios do not cover every dimension")
    return scenarios


def _discovery(protocol: dict[str, Any], result_path: Path) -> int:
    from quant_runtime.adapters.discovery.qlib.adapter import (
        QlibStrategyError,
        run_qlib_discovery_frame,
    )
    from quant_runtime.adapters.discovery.qlib.capsule import load_discovery_capsule
    from quant_runtime.artifacts import sha256_value

    config = protocol.get("phase_config")
    package = protocol.get("package")
    if (
        not isinstance(config, dict)
        or config.get("adapter") != "qlib"
        or not isinstance(config.get("entrypoint"), str)
        or not isinstance(config.get("snapshot_id"), str)
        or not isinstance(package, dict)
        or not isinstance(package.get("package_hash"), str)
    ):
        _write(
            result_path,
            _result(protocol, "policy_rejection", {"code": "discovery_protocol_invalid"}),
        )
        return 0
    try:
        parameters = _read(Path("/sandbox/inputs/parameters.json"))
        if sha256_value(parameters) != protocol.get("parameters_hash"):
            raise ValueError("sandbox parameters identity differs")
    except Exception:
        _write(
            result_path,
            _result(protocol, "policy_rejection", {"code": "discovery_input_invalid"}),
        )
        return 0
    try:
        frame = load_discovery_capsule(
            Path("/sandbox/inputs/discovery-capsule.json"),
            snapshot_id=config["snapshot_id"],
        )
        result = run_qlib_discovery_frame(
            package_root=Path("/sandbox/package"),
            entrypoint=config["entrypoint"],
            package_hash=package["package_hash"],
            parameters=parameters,
            snapshot_id=config["snapshot_id"],
            frame=frame,
            output=result_path.parent,
        )
    except QlibStrategyError:
        _write(
            result_path,
            _result(protocol, "strategy_rejection", {"code": "qlib_strategy_rejected"}),
        )
        return 0
    except Exception:
        _write(
            result_path,
            _result(protocol, "engine_failure", {"code": "qlib_engine_failed"}),
        )
        return 0
    _write(
        result_path,
        _result(
            protocol,
            "success",
            {
                "backend_id": result.backend_id,
                "adapter_version": result.adapter_version,
                "engine_version": result.engine_version,
                "artifact_hash": result.artifact_hash,
                "metrics": result.metrics,
                "evidence": list(result.evidence),
            },
        ),
    )
    return 0


def _formal(protocol: dict[str, Any], result_path: Path) -> int:
    from quant_runtime.adapters.formal.nautilus.adapter import (
        NautilusStrategyError,
        NautilusWorkspaceAdapter,
    )
    from quant_runtime.adapters.interface import FormalRunInput
    from quant_runtime.artifacts import sha256_value
    from quant_runtime.package import StrategyPackage
    from quant_runtime.sandbox.snapshot import load_snapshot_capsule

    config = protocol.get("phase_config")
    package_ref = protocol.get("package")
    package_manifest = protocol.get("package_manifest")
    if (
        not isinstance(config, dict)
        or config.get("adapter") != "nautilus"
        or not isinstance(config.get("formal_id"), str)
        or not isinstance(config.get("snapshot_id"), str)
        or not isinstance(config.get("config"), dict)
        or not isinstance(package_ref, dict)
        or not isinstance(package_manifest, dict)
    ):
        _write(
            result_path, _result(protocol, "policy_rejection", {"code": "formal_protocol_invalid"})
        )
        return 0
    try:
        parameters = _read(Path("/sandbox/inputs/parameters.json"))
        if sha256_value(parameters) != protocol.get("parameters_hash"):
            raise ValueError("sandbox parameters identity differs")
        snapshot = load_snapshot_capsule(
            Path("/sandbox/inputs/snapshot-capsule.json"),
            snapshot_id=config["snapshot_id"],
        )
        package = StrategyPackage.from_record(
            {"package_ref": package_ref, "manifest": package_manifest},
            Path("/sandbox/package"),
        )
        if package.resolve_entrypoint("formal", "nautilus") != config.get("entrypoint"):
            raise ValueError("sandbox formal entrypoint identity differs")
    except Exception:
        _write(result_path, _result(protocol, "policy_rejection", {"code": "formal_input_invalid"}))
        return 0
    try:
        result = NautilusWorkspaceAdapter().run(
            FormalRunInput(
                package=package,
                parameters=parameters,
                snapshot=snapshot,
                output=result_path.parent,
                config=config["config"],
                cache_path=None,
                cache_policy=str(config.get("cache_policy", "none")),
                cache_transform_version=None,
            ),
            formal_id=config["formal_id"],
        )
    except NautilusStrategyError:
        _write(
            result_path,
            _result(protocol, "strategy_rejection", {"code": "nautilus_strategy_rejected"}),
        )
        return 0
    except Exception as exc:
        strategy_owned = _originated_in_package(exc)
        _write(
            result_path,
            _result(
                protocol,
                "strategy_rejection" if strategy_owned else "engine_failure",
                {
                    "code": (
                        "nautilus_strategy_rejected" if strategy_owned else "nautilus_engine_failed"
                    )
                },
            ),
        )
        return 0
    _write(result_path, _result(protocol, "success", result.as_contract()))
    return 0


def _result(
    protocol: dict[str, Any], classification: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": "quant-runtime.sandbox-worker-result.v1",
        "invocation_id": protocol["invocation_id"],
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


def _originated_in_package(exc: BaseException) -> bool:
    package_root = Path("/sandbox/package")
    return any(
        _is_relative_to(Path(frame.f_code.co_filename), package_root)
        for frame, _ in traceback.walk_tb(exc.__traceback__)
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("sandbox worker input must be an object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
