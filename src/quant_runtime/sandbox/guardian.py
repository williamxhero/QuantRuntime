from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Sequence

import psutil


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-runtime-sandbox-guardian")
    parser.add_argument("--docker", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--parent-created", required=True, type=float)
    arguments = parser.parse_args(argv)
    while True:
        if not _same_process(arguments.parent_pid, arguments.parent_created):
            _control(arguments.docker, "kill", arguments.container)
            _control(arguments.docker, "rm", "--force", arguments.container)
            return 0
        state = _state(arguments.docker, arguments.container)
        if state is None:
            return 0
        if state.get("Running") is False and state.get("Status") != "created":
            return 0
        time.sleep(0.1)


def _same_process(pid: int, created: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - created) < 0.001
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _state(docker: str, container: str) -> dict | None:
    completed = _control(docker, "inspect", "--format", "{{json .State}}", container)
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _control(docker: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker, *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        shell=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
