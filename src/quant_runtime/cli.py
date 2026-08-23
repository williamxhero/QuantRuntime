from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from quant_runtime.application import (
    run_discover,
    run_evaluate,
    run_golden_check,
    run_package_validate,
    run_snapshot_resolve,
    run_workspace,
)
from quant_runtime.formal import formal_runtime_names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="run Qlib candidate discovery")
    discover.add_argument("--config", type=Path, required=True)
    discover.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="run formal evaluation")
    evaluate.add_argument("--candidate-manifest", type=Path, required=True)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument(
        "--runtime",
        choices=formal_runtime_names(),
        default="nautilus",
        help="formal runtime adapter (default: nautilus)",
    )
    golden = commands.add_parser("golden-check", help="compare candidate and formal semantics")
    golden.add_argument("--candidate-manifest", type=Path, required=True)
    golden.add_argument("--formal-manifest", type=Path, required=True)
    golden.add_argument("--output", type=Path)
    package_validate = commands.add_parser(
        "package-validate", help="validate a Strategy Package and frozen parameters"
    )
    package_validate.add_argument("--package", type=Path, required=True)
    package_validate.add_argument("--parameters", type=Path)
    snapshot_resolve = commands.add_parser(
        "snapshot-resolve", help="resolve a MarketHub reference or materialized snapshot"
    )
    snapshot_resolve.add_argument("--request", type=Path, required=True)
    snapshot_resolve.add_argument("--runtime-root", type=Path, default=Path(".runtime"))
    workspace_run = commands.add_parser("run", help="execute a Strategy Workspace run request")
    workspace_run.add_argument("--request", type=Path, required=True)
    workspace_run.add_argument("--runtime-root", type=Path, default=Path(".runtime"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            result = run_discover(args.config, args.output)
        elif args.command == "evaluate":
            result = run_evaluate(
                args.candidate_manifest,
                args.config,
                args.output,
                runtime_name=args.runtime,
            )
        elif args.command == "golden-check":
            result = run_golden_check(
                args.candidate_manifest,
                args.formal_manifest,
                args.output,
            )
        elif args.command == "package-validate":
            result = run_package_validate(args.package, args.parameters)
        elif args.command == "snapshot-resolve":
            result = run_snapshot_resolve(args.request, args.runtime_root)
        else:
            result = run_workspace(args.request, args.runtime_root)
    except Exception as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        result_payload = {"status": "failed", "error": str(exc)}
        result_code = 2
    else:
        result_payload = result.payload
        result_code = result.exit_code
    print(json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")))
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
