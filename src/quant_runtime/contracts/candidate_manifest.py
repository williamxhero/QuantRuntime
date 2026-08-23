from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical_hash import Artifact, read_json

CANDIDATE_SCHEMA = "quant-runtime.candidate-manifest.v1"


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    path: Path
    raw: dict[str, Any]
    run_id: str
    status: str
    data_version: str
    dataset_version: str
    config_hash: str
    strategy_spec_hash: str
    canonical_input_hash: str
    reference_decision_hash: str
    artifacts: tuple[Artifact, ...]

    @classmethod
    def load(cls, path: Path, *, verify_artifacts: bool = True) -> CandidateManifest:
        raw = read_json(path)
        if raw.get("schema") != CANDIDATE_SCHEMA:
            raise ValueError(f"unsupported candidate manifest schema {raw.get('schema')!r}")
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("candidate manifest metrics must be an object")
        artifact_rows = raw.get("artifacts")
        if not isinstance(artifact_rows, list) or any(
            not isinstance(row, dict) for row in artifact_rows
        ):
            raise ValueError("candidate manifest artifacts must be a list of objects")
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
            reference_decision_hash=str(metrics.get("reference_decision_hash", "")),
            artifacts=tuple(Artifact.from_dict(row) for row in artifact_rows),
        )
        manifest.validate()
        if verify_artifacts:
            for artifact in manifest.artifacts:
                artifact.verify(manifest.path)
        return manifest

    def validate(self) -> None:
        if self.status != "passed":
            raise ValueError(f"candidate status must be passed, got {self.status!r}")
        required_hashes = (
            self.config_hash,
            self.strategy_spec_hash,
            self.canonical_input_hash,
            self.reference_decision_hash,
        )
        if not self.run_id or any(len(value) != 64 for value in required_hashes):
            raise ValueError("candidate manifest identity fields are incomplete")

    def artifact_path(self, relative_path: str) -> Path:
        artifact = next(
            (item for item in self.artifacts if item.relative_path == relative_path), None
        )
        if artifact is None:
            raise ValueError(f"candidate manifest lacks artifact {relative_path!r}")
        return artifact.verify(self.path)
