from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quant_runtime.contracts.candidate_manifest import CandidateManifest
from quant_runtime.contracts.canonical_hash import Artifact, sha256_bytes, write_json
from quant_runtime.contracts.strategy_spec import StrategySpec
from quant_runtime.semantics.decision_record import (
    DecisionRecord,
    canonical_weight,
    decision_envelope,
    decision_hash,
)

ROOT = Path(__file__).parents[2]


def test_strategy_hash_and_decision_order_are_canonical() -> None:
    spec = StrategySpec.load(ROOT / "configs" / "strategies" / "cross-sectional-momentum-topk.json")
    assert len(spec.spec_hash) == 64
    decisions = [
        DecisionRecord(date(2025, 1, 2), "SZ.000001", canonical_weight(3), Decimal("1")),
        DecisionRecord(date(2025, 1, 2), "SH.600000", canonical_weight(3), Decimal("1")),
        DecisionRecord(date(2025, 1, 2), "BJ.430001", canonical_weight(3), Decimal("2")),
    ]
    envelope = decision_envelope(decisions, spec.spec_hash)
    assert [item["instrument"] for item in envelope["decisions"]] == [
        "BJ.430001",
        "SH.600000",
        "SZ.000001",
    ]
    assert {item["target_weight"] for item in envelope["decisions"]} == {"0.333333333333"}
    assert len(decision_hash(envelope)) == 64


def test_artifact_verification_rejects_tampering(tmp_path: Path) -> None:
    artifact_path = tmp_path / "strategy_decisions.json"
    write_json(artifact_path, {"value": 1})
    artifact = Artifact.from_path(artifact_path, tmp_path)
    artifact_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity mismatch"):
        artifact.verify(tmp_path / "candidate_manifest.json")


def test_candidate_loader_enforces_public_schema_and_artifacts(tmp_path: Path) -> None:
    decisions = write_json(tmp_path / "strategy_decisions.json", {"value": 1})
    artifact = Artifact.from_path(decisions, tmp_path).as_dict()
    manifest = {
        "schema": "quant-runtime.candidate-manifest.v1",
        "run_id": "run",
        "status": "passed",
        "data_version": "data",
        "dataset_version": "dataset",
        "config_hash": "a" * 64,
        "strategy_spec_hash": "b" * 64,
        "canonical_input_hash": "c" * 64,
        "artifacts": [artifact],
        "metrics": {"reference_decision_hash": "d" * 64},
    }
    path = write_json(tmp_path / "candidate_manifest.json", manifest)
    loaded = CandidateManifest.load(path)
    assert loaded.artifact_path("strategy_decisions.json") == decisions.resolve()
    assert sha256_bytes(decisions.read_bytes()) == artifact["sha256"]
    assert json.loads(path.read_text())["schema"] == "quant-runtime.candidate-manifest.v1"
