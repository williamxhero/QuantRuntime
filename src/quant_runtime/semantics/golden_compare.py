from __future__ import annotations

from pathlib import Path
from typing import Any

from quant_runtime.contracts.candidate_manifest import CandidateManifest
from quant_runtime.contracts.canonical_hash import sha256_bytes, write_json
from quant_runtime.contracts.formal_manifest import FormalManifest


def compare_manifests(candidate_path: Path, formal_path: Path) -> dict[str, Any]:
    candidate = CandidateManifest.load(candidate_path)
    formal = FormalManifest.load(formal_path)
    checks = {
        "candidate_run_id": candidate.run_id == formal.candidate_run_id,
        "candidate_manifest_hash": sha256_bytes(candidate.path.read_bytes())
        == formal.candidate_manifest_hash,
        "data_version": candidate.data_version == formal.data_version,
        "dataset_version": candidate.dataset_version == formal.dataset_version,
        "strategy_spec_hash": candidate.strategy_spec_hash == formal.strategy_spec_hash,
        "canonical_input_hash": candidate.canonical_input_hash == formal.canonical_input_hash,
        "decision_hash": candidate.reference_decision_hash == formal.formal_decision_hash,
        "formal_semantic_match": formal.semantic_match,
    }
    matched = all(checks.values())
    return {
        "schema": "quant-runtime.golden-check.v1",
        "status": "matched" if matched else "mismatched",
        "semantic_match": matched,
        "candidate_run_id": candidate.run_id,
        "formal_run_id": formal.run_id,
        "checks": checks,
    }


def write_golden_report(output: Path, report: dict[str, Any]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    return write_json(output / "golden_check.json", report).resolve()
