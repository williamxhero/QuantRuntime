from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from quant_runtime.application import run_discover, run_evaluate, run_golden_check
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
        else:
            result = run_golden_check(
                args.candidate_manifest,
                args.formal_manifest,
                args.output,
            )
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
