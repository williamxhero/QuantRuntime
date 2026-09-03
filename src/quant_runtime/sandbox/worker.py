from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
        blocked = False
        try:
            process = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], shell=False)
            process.kill()
        except OSError:
            blocked = True
        _write(result_path, _result(protocol, "success", {"spawn_blocked": blocked}))
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
