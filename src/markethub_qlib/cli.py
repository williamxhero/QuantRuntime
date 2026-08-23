from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import qlib

from .artifacts import write_failed_run, write_successful_run
from .config import RunConfig
from .workflow import run_discovery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="markethub-qlib")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run a Qlib discovery experiment")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = RunConfig.load(args.config)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    try:
        result = run_discovery(config)
        written = write_successful_run(config, result, args.output)
        exit_code = 0 if written.status == "passed" else 1
    except Exception as exc:
        written = write_failed_run(
            config,
            args.output,
            exc,
            framework_version=qlib.__version__,
        )
        print(f"discovery failed: {exc}", file=sys.stderr)
        exit_code = 2
    print(
        json.dumps(
            {
                "status": written.status,
                "run_id": written.run_id,
                "manifest_path": str(written.manifest_path),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
