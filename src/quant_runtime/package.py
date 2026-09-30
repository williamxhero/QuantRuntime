from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_runtime.artifacts import sha256_value

SIGNAL_CAPABILITY = "data.bar.1d"
SIGNAL_FREQUENCY = "1d"
SIGNAL_ADJUSTMENTS = frozenset({"none", "hfq"})
REFUSED_SIGNAL_ADJUSTMENTS = frozenset({"qfq", "offset"})


class SignalSeriesUnavailable(RuntimeError):
    """A declared back-adjusted signal series could not be served honestly."""


@dataclass(frozen=True, slots=True)
class StrategyPackage:
    """Executor view of a package already validated and registered by Strategy Workspace."""

    root: Path
    package_ref: dict[str, Any]
    manifest: dict[str, Any]

    @classmethod
    def from_record(cls, record: dict[str, Any], root: Path) -> StrategyPackage:
        package_ref = record.get("package_ref")
        manifest = record.get("manifest")
        if not isinstance(package_ref, dict) or not isinstance(manifest, dict):
            raise ValueError("workspace package record lacks package_ref or manifest")
        source = Path(root).resolve()
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
    def asset_classes(self) -> frozenset[str]:
        return self._requirement_values("asset_classes")

    @property
    def frequencies(self) -> frozenset[str]:
        return self._requirement_values("frequencies")

    @property
    def decision_intents(self) -> frozenset[str]:
        return self._requirement_values("decision_intents")

    @property
    def discovery_policy(self) -> str:
        pipeline = self.manifest.get("pipeline", {})
        if not isinstance(pipeline, dict):
            raise ValueError("strategy package pipeline must be an object")
        return str(pipeline.get("discovery", "optional"))

    @property
    def signal_adjustment(self) -> str:
        """The adjustment the strategy reads signals from: ``none`` or ``hfq``.

        Strategy Workspace owns the declaration shape, so this reads the v2
        ``requirements.data`` array of ``{capability, frequency, adjustment}``
        entries.  A package that declares no daily-bar entry -- every V1.1 object --
        reads raw prices, exactly as before.
        """

        requirements = self.manifest.get("requirements", {})
        if not isinstance(requirements, dict):
            raise ValueError("strategy package requirements must be an object")
        data = requirements.get("data")
        if data is None:
            return "none"
        if not isinstance(data, list):
            raise SignalSeriesUnavailable(
                "strategy package requirements.data must be an array of data declarations"
            )
        declared = [
            item
            for item in data
            if isinstance(item, dict)
            and str(item.get("capability")) == SIGNAL_CAPABILITY
            and str(item.get("frequency")) == SIGNAL_FREQUENCY
        ]
        adjustments = {str(item.get("adjustment")) for item in declared}
        if refused := sorted(adjustments & REFUSED_SIGNAL_ADJUSTMENTS):
            raise SignalSeriesUnavailable(
                f"daily back-adjusted signals support 'hfq' only; declared {refused}"
            )
        if unsupported := sorted(adjustments - SIGNAL_ADJUSTMENTS):
            raise SignalSeriesUnavailable(
                f"unsupported daily signal adjustment declared: {unsupported}"
            )
        return "hfq" if "hfq" in adjustments else "none"

    def require_signal_series(self, snapshot_adjustment: str) -> None:
        """Refuse a declared signal series the resolved snapshot cannot carry."""

        if self.signal_adjustment != "hfq":
            return
        if snapshot_adjustment != "hfq":
            raise SignalSeriesUnavailable(
                "strategy package declares a back-adjusted daily signal series "
                f"('{SIGNAL_CAPABILITY}' adjustment 'hfq') but the snapshot is "
                f"{snapshot_adjustment!r}; re-run against an hfq snapshot"
            )

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

    def _requirement_values(self, name: str) -> frozenset[str]:
        requirements = self.manifest.get("requirements", {})
        if not isinstance(requirements, dict):
            raise ValueError("strategy package requirements must be an object")
        values = requirements.get(name, [])
        if not isinstance(values, list):
            raise ValueError(f"strategy package requirements.{name} must be an array")
        return frozenset(str(item) for item in values)
