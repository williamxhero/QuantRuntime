from __future__ import annotations

from pathlib import Path

from quant_runtime.semantics.golden_compare import compare_manifests, write_golden_report

from .result import ApplicationResult


def run_golden_check(
    candidate_manifest: Path,
    formal_manifest: Path,
    output: Path | None = None,
) -> ApplicationResult:
    report = compare_manifests(candidate_manifest, formal_manifest)
    path = write_golden_report(output or formal_manifest.resolve().parent, report)
    return ApplicationResult(
        payload={
            "status": report["status"],
            "candidate_run_id": report["candidate_run_id"],
            "formal_run_id": report["formal_run_id"],
            "report_path": str(path),
        },
        exit_code=0 if report["semantic_match"] else 1,
    )
