from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_runtime.artifacts import sha256_value


@dataclass(frozen=True, slots=True)
class StrategyPackage:
    """Executor view of a package already validated and registered by Strategy Workspace."""

    root: Path
    package_ref: dict[str, Any]
    manifest: dict[str, Any]

    @classmethod
    def from_record(cls, record: dict[str, Any], root: Path | None = None) -> StrategyPackage:
        package_ref = record.get("package_ref")
        manifest = record.get("manifest")
        if not isinstance(package_ref, dict) or not isinstance(manifest, dict):
            raise ValueError("workspace package record lacks package_ref or manifest")
        source = root or Path(str(record.get("source_path", "")))
        source = source.resolve()
        if not source.is_dir():
            raise ValueError("registered strategy package is not materialized for execution")
        if manifest.get("strategy_id") != package_ref.get("strategy_id") or manifest.get(
            "revision"
        ) != package_ref.get("revision"):
            raise ValueError("workspace package manifest and package ref differ")
        return cls(source, dict(package_ref), dict(manifest))

    @property
    def strategy_id(self) -> str:
        return str(self.package_ref["strategy_id"])

    @property
    def revision(self) -> int:
        return int(self.package_ref["revision"])

    @property
    def package_hash(self) -> str:
        return str(self.package_ref["package_hash"])

    @property
    def requirements(self) -> frozenset[str]:
        requirements = self.manifest.get("requirements", {})
        if not isinstance(requirements, dict):
            raise ValueError("strategy package requirements must be an object")
        return frozenset(str(item) for item in requirements.get("capabilities", []))

    @property
    def discovery_policy(self) -> str:
        pipeline = self.manifest.get("pipeline", {})
        if not isinstance(pipeline, dict):
            raise ValueError("strategy package pipeline must be an object")
        return str(pipeline.get("discovery", "optional"))

    def implementations(self, role: str) -> dict[str, str]:
        implementations = self.manifest.get("implementations", {})
        if not isinstance(implementations, dict):
            raise ValueError("strategy package implementations must be an object")
        values = implementations.get(role, {})
        if not isinstance(values, dict):
            raise ValueError(f"strategy package {role} implementations must be an object")
        return {str(key): str(value) for key, value in values.items()}

    def parameters_hash(self, parameters: dict[str, Any]) -> str:
        return sha256_value(parameters)

    def resolve_entrypoint(self, role: str, backend_id: str) -> str:
        try:
            return self.implementations(role)[backend_id]
        except KeyError as exc:
            raise ValueError(
                f"strategy package has no {role} implementation for {backend_id!r}"
            ) from exc
