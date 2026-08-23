from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from quant_runtime.discovery.candidate_manifest import write_candidate_run
from quant_runtime.discovery.workflow import DiscoveryConfig, run_discovery
from quant_runtime.formal.runner import evaluate_candidate
from quant_runtime.semantics.golden_compare import compare_manifests, write_golden_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="run Qlib candidate discovery")
    discover.add_argument("--config", type=Path, required=True)
    discover.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="run Nautilus formal evaluation")
    evaluate.add_argument("--candidate-manifest", type=Path, required=True)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    golden = commands.add_parser("golden-check", help="compare candidate and formal semantics")
    golden.add_argument("--candidate-manifest", type=Path, required=True)
    golden.add_argument("--formal-manifest", type=Path, required=True)
    golden.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            config = DiscoveryConfig.load(args.config)
            result = run_discovery(config)
            manifest, path = write_candidate_run(config, result, args.output)
            payload = {
                "status": manifest["status"],
                "run_id": manifest["run_id"],
                "manifest_path": str(path),
            }
            exit_code = 0 if manifest["status"] == "passed" else 1
        elif args.command == "evaluate":
            manifest, path = evaluate_candidate(args.candidate_manifest, args.config, args.output)
            payload = {
                "status": manifest["status"],
                "run_id": manifest["run_id"],
                "manifest_path": str(path),
            }
            exit_code = 0 if manifest["status"] == "matched" else 1
        else:
            report = compare_manifests(args.candidate_manifest, args.formal_manifest)
            output = args.output or args.formal_manifest.resolve().parent
            path = write_golden_report(output, report)
            payload = {
                "status": report["status"],
                "candidate_run_id": report["candidate_run_id"],
                "formal_run_id": report["formal_run_id"],
                "report_path": str(path),
            }
            exit_code = 0 if report["semantic_match"] else 1
    except Exception as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        payload = {"status": "failed", "error": str(exc)}
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
