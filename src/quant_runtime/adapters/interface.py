from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from quant_runtime.adapters.data.markethub import ResolvedSnapshot
from quant_runtime.sdk.package_manifest import StrategyPackage


@dataclass(frozen=True, slots=True)
class DiscoveryRunInput:
    package: StrategyPackage
    parameters: dict[str, Any]
    snapshot: ResolvedSnapshot
    output: Path
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DiscoveryAdapterResult:
    backend_id: str
    adapter_version: str
    engine_version: str
    artifact_hash: str
    metrics: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class FormalRunInput:
    """Neutral formal input. Discovery candidates are deliberately absent."""

    package: StrategyPackage
    parameters: dict[str, Any]
    snapshot: ResolvedSnapshot
    output: Path
    config: dict[str, Any]
    cache_path: Path | None
    cache_policy: str
    cache_transform_version: str | None


@dataclass(frozen=True, slots=True)
class FormalAdapterResult:
    backend_id: str
    adapter_version: str
    engine_version: str
    status: str
    metrics: dict[str, Any]
    positions: tuple[dict[str, Any], ...]
    fills: tuple[dict[str, Any], ...]
    account_curve: tuple[dict[str, Any], ...]
    native_evidence: tuple[dict[str, Any], ...]

    def as_contract(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "adapter_version": self.adapter_version,
            "engine_version": self.engine_version,
            "status": self.status,
            "metrics": self.metrics,
            "positions": list(self.positions),
            "fills": list(self.fills),
            "account_curve": list(self.account_curve),
            "native_evidence": list(self.native_evidence),
        }


class DiscoveryAdapter(Protocol):
    name: str

    def run(self, value: DiscoveryRunInput) -> DiscoveryAdapterResult: ...


class FormalAdapter(Protocol):
    name: str

    def run(self, value: FormalRunInput) -> FormalAdapterResult: ...
