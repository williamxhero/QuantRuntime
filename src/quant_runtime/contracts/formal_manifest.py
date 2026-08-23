from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical_hash import Artifact, read_json

FORMAL_SCHEMA = "quant-runtime.formal-manifest.v1"


@dataclass(frozen=True, slots=True)
class FormalManifest:
    path: Path
    raw: dict[str, Any]
    run_id: str
    status: str
    data_version: str
    dataset_version: str
    config_hash: str
    strategy_spec_hash: str
    canonical_input_hash: str
    normalized_output_hash: str
    candidate_run_id: str
    candidate_manifest_hash: str
    candidate_decision_hash: str
    formal_decision_hash: str
    semantic_match: bool
    artifacts: tuple[Artifact, ...]

    @classmethod
    def load(cls, path: Path, *, verify_artifacts: bool = True) -> FormalManifest:
        raw = read_json(path)
        if raw.get("schema") != FORMAL_SCHEMA:
            raise ValueError(f"unsupported formal manifest schema {raw.get('schema')!r}")
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("formal manifest metrics must be an object")
        artifact_rows = raw.get("artifacts")
        if not isinstance(artifact_rows, list) or any(
            not isinstance(row, dict) for row in artifact_rows
        ):
            raise ValueError("formal manifest artifacts must be a list of objects")
        manifest = cls(
            path=path.resolve(),
            raw=raw,
            run_id=str(raw.get("run_id", "")),
            status=str(raw.get("status", "")),
            data_version=str(raw.get("data_version", "")),
            dataset_version=str(raw.get("dataset_version", "")),
            config_hash=str(raw.get("config_hash", "")),
            strategy_spec_hash=str(raw.get("strategy_spec_hash", "")),
            canonical_input_hash=str(raw.get("canonical_input_hash", "")),
            normalized_output_hash=str(raw.get("normalized_output_hash", "")),
            candidate_run_id=str(raw.get("candidate_run_id", "")),
            candidate_manifest_hash=str(raw.get("candidate_manifest_hash", "")),
            candidate_decision_hash=str(metrics.get("candidate_decision_hash", "")),
            formal_decision_hash=str(metrics.get("formal_decision_hash", "")),
            semantic_match=metrics.get("semantic_match") is True,
            artifacts=tuple(Artifact.from_dict(row) for row in artifact_rows),
        )
        manifest.validate()
        if verify_artifacts:
            for artifact in manifest.artifacts:
                artifact.verify(manifest.path)
        return manifest

    def validate(self) -> None:
        required_hashes = (
            self.config_hash,
            self.strategy_spec_hash,
            self.canonical_input_hash,
            self.normalized_output_hash,
            self.candidate_manifest_hash,
            self.candidate_decision_hash,
            self.formal_decision_hash,
        )
        if (
            not self.run_id
            or not self.candidate_run_id
            or any(len(value) != 64 for value in required_hashes)
        ):
            raise ValueError("formal manifest identity fields are incomplete")
        if self.status not in {"matched", "mismatched"}:
            raise ValueError(f"invalid formal status {self.status!r}")
        if self.semantic_match != (self.status == "matched"):
            raise ValueError("formal status and metrics.semantic_match disagree")
