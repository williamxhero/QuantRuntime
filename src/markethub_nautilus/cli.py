from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import run, write_failed_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="markethub-nautilus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run a MarketHub-backed Nautilus backtest")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest, path = run(args.config.resolve(), args.output.resolve())
    except Exception as exc:  # CLI boundary intentionally converts failures to evidence.
        try:
            manifest, path = write_failed_run(args.config.resolve(), args.output.resolve(), exc)
        except Exception:
            print(f"markethub-nautilus failed: {exc}", file=sys.stderr)
            return 2
        print(str(exc), file=sys.stderr)
        print(
            json.dumps(
                {
                    "manifest_path": str(path.resolve()),
                    "run_id": manifest["run_id"],
                    "status": manifest["status"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {
                "manifest_path": str(path.resolve()),
                "run_id": manifest["run_id"],
                "status": manifest["status"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
