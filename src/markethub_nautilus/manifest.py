from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import __version__ as tool_version
from .canonical import canonical_json


@dataclass(frozen=True, slots=True)
class Artifact:
    relative_path: str
    sha256: str
    content_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_bytes": self.content_bytes,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
        }


def artifact_for(path: Path, root: Path) -> Artifact:
    content = path.read_bytes()
    return Artifact(
        relative_path=path.relative_to(root).as_posix(),
        sha256=sha256(content).hexdigest(),
        content_bytes=len(content),
    )


def build_run_id(
    config_hash: str,
    strategy_spec_hash: str,
    canonical_input_hash: str | None,
    framework_version: str,
) -> str:
    identity = {
        "canonical_input_hash": canonical_input_hash,
        "config_hash": config_hash,
        "framework": "nautilus_trader",
        "framework_version": framework_version,
        "strategy_spec_hash": strategy_spec_hash,
        "tool_version": tool_version,
    }
    return f"nt-{sha256(canonical_json(identity)).hexdigest()[:24]}"


def write_manifest(
    output_dir: Path,
    *,
    framework_version: str,
    status: str,
    data_version: str | None,
    config_hash: str,
    strategy_spec_hash: str,
    canonical_input_hash: str | None,
    normalized_output_hash: str | None,
    artifact_paths: list[Path],
    metrics: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = build_run_id(
        config_hash,
        strategy_spec_hash,
        canonical_input_hash,
        framework_version,
    )
    manifest: dict[str, Any] = {
        "artifacts": [
            item.as_dict()
            for item in sorted(
                (artifact_for(path, output_dir) for path in artifact_paths),
                key=lambda item: item.relative_path,
            )
        ],
        "canonical_input_hash": canonical_input_hash,
        "config_hash": config_hash,
        "data_version": data_version,
        "framework": "nautilus_trader",
        "framework_version": framework_version,
        "normalized_output_hash": normalized_output_hash,
        "run_id": run_id,
        "schema": "markethub-nautilus.run-manifest.v1",
        "status": status,
        "strategy_spec_hash": strategy_spec_hash,
        "tool_version": tool_version,
    }
    if metrics is not None:
        manifest["metrics"] = metrics
    if error is not None:
        manifest["error"] = error
    path = output_dir / "run_manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest, path
