from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_runtime.adapters.data.markethub import MarketHubDataAdapter, ResolvedSnapshot
from quant_runtime.adapters.interface import DiscoveryRunInput, FormalAdapterResult, FormalRunInput
from quant_runtime.contracts.canonical_hash import (
    artifact_records,
    read_json,
    sha256_value,
    write_json,
)
from quant_runtime.sdk.capability_contract import AdapterRegistry
from quant_runtime.sdk.package_manifest import StrategyPackage
from quant_runtime.sdk.package_manifest import validate_package as load_package
from quant_runtime.sdk.result_contract import validate_result
from quant_runtime.sdk.run_manifest import validate_run_manifest, validate_run_request
from quant_runtime.sdk.snapshot_contract import SnapshotRequest

from .atomic import AtomicDirectory
from .comparison import compare_results
from .layout import RuntimeLayout
from .registry import production_registry


class StrategyWorkspace:
    """Validate a package, resolve one frozen input, and execute an explicit topology."""

    def __init__(
        self,
        runtime_root: Path = Path(".runtime"),
        *,
        registry: AdapterRegistry | None = None,
        data_adapter: MarketHubDataAdapter | None = None,
    ) -> None:
        self.layout = RuntimeLayout.create(runtime_root)
        self.registry = registry or production_registry()
        self.data_adapter = data_adapter or MarketHubDataAdapter()

    def validate_package(
        self,
        path: Path,
        parameters: dict[str, Any] | None = None,
    ) -> StrategyPackage:
        package = load_package(path)
        package.resolve_parameters(parameters)
        return package

    def resolve_snapshot(self, value: dict[str, Any]) -> ResolvedSnapshot:
        request = SnapshotRequest.from_dict(value)
        return self.data_adapter.resolve(request, self.layout)

    def run(
        self,
        request: dict[str, Any],
        *,
        request_root: Path | None = None,
    ) -> tuple[dict[str, Any], Path]:
        validate_run_request(request)
        root = (request_root or Path.cwd()).resolve()
        package_path = Path(str(request["package"]))
        if not package_path.is_absolute():
            package_path = root / package_path
        package = self.validate_package(package_path, request.get("parameters"))
        parameters = package.resolve_parameters(request.get("parameters"))
        snapshot_request = SnapshotRequest.from_dict(request["data"])
        snapshot = self.data_adapter.resolve(snapshot_request, self.layout)
        if snapshot.dataset is None:
            verified = self.data_adapter.read(
                snapshot_request,
                expected_revision=str(snapshot.manifest["source"]["data_revision"]),
            )
            snapshot = replace(snapshot, dataset=verified.dataset)
        discovery_backend = self._resolve_discovery(package, request.get("discovery"))
        selection = self.registry.resolve_formal(
            request["formal"],
            package.requirements,
            package.implementations("formal"),
        )
        discovery_request = request.get("discovery") or {"mode": "skip"}
        discovery_profile = (
            self.registry.profile("discovery", discovery_backend)
            if discovery_backend is not None
            else None
        )
        formal_profiles = tuple(
            self.registry.profile("formal", backend_id) for backend_id in selection.backend_ids
        )
        run_identity = {
            "strategy_package_hash": package.package_hash,
            "parameters_hash": package.parameters_hash(parameters),
            "snapshot_id": snapshot.snapshot_id,
            "request_semantics": {
                "data": {
                    **snapshot_request.identity_payload(),
                    "snapshot_mode": snapshot_request.snapshot_mode,
                    "trust_policy": snapshot_request.trust_policy,
                    "local_cache": snapshot_request.local_cache,
                },
                "discovery": {
                    **discovery_request,
                    "resolved_backend": discovery_backend,
                },
                "formal": {
                    **request["formal"],
                    "resolved_backends": selection.backend_ids,
                    "resolved_minimum_agreement": selection.minimum_agreement,
                },
            },
            "snapshot_source": snapshot.manifest["source"],
            "read_method": (
                "materialized_parquet" if snapshot.mode == "materialized" else "direct_markethub"
            ),
            "capability_profiles": {
                "data": {
                    "backend_id": self.data_adapter.name,
                    "adapter_version": self.data_adapter.adapter_version,
                },
                "discovery": (
                    _profile_identity(discovery_profile) if discovery_profile is not None else None
                ),
                "formal": [_profile_identity(item) for item in formal_profiles],
            },
        }
        run_id = f"qr-workspace-{sha256_value(run_identity)[:24]}"
        final = self.layout.runs / run_id
        existing = final / "run_manifest.json"
        if existing.exists():
            manifest = read_json(existing)
            validate_run_manifest(manifest)
            return manifest, existing.resolve()
        with AtomicDirectory(self.layout.staging, final) as staging:
            discovery_hash = None
            if discovery_backend is not None:
                discovery_adapter = self.registry.create("discovery", discovery_backend)
                discovery_result = discovery_adapter.run(
                    DiscoveryRunInput(
                        package,
                        parameters,
                        snapshot,
                        staging.path / "discovery" / discovery_backend,
                        dict((request.get("discovery") or {}).get("config", {})),
                    )
                )
                discovery_hash = discovery_result.artifact_hash
            formal_results = tuple(
                self._run_formal(
                    backend_id,
                    package,
                    parameters,
                    snapshot,
                    staging.path / "formal" / backend_id,
                    dict(request["formal"].get("config", {})),
                    run_id,
                    snapshot_request.local_cache,
                )
                for backend_id in selection.backend_ids
            )
            comparison = compare_results(
                selection.mode,
                formal_results,
                agreement=request["formal"].get("agreement"),
                minimum_agreement=selection.minimum_agreement,
            )
            status = (
                "rejected"
                if comparison is not None and comparison.get("passed") is False
                else "completed"
            )
            result = {
                "schema": "quant-research.result.v1",
                "run_id": run_id,
                "status": status,
                "formal_results": [item.as_contract() for item in formal_results],
                "comparison": comparison,
                "warnings": [],
                "incomparable": [],
            }
            validate_result(result)
            result_path = write_json(staging.path / "result.json", result)
            evidence_paths = sorted(
                path for path in staging.path.rglob("*") if path.is_file() and path != result_path
            )
            evidence = artifact_records(staging.path, evidence_paths)
            result_record = artifact_records(staging.path, [result_path])[0]
            manifest = {
                "schema": "quant-research.run-manifest.v1",
                "run_id": run_id,
                "status": status,
                "created_at": _now(),
                "strategy": {
                    "strategy_id": package.strategy_id,
                    "revision": package.revision,
                    "package_hash": package.package_hash,
                },
                "parameters": {
                    "values": parameters,
                    "hash": package.parameters_hash(parameters),
                },
                "snapshot": {
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_mode": snapshot.mode,
                    "manifest_path": str(snapshot.manifest_path),
                    "adapter_version": self.data_adapter.adapter_version,
                    "local_cache": snapshot_request.local_cache,
                    "read_method": (
                        "materialized_parquet"
                        if snapshot.mode == "materialized"
                        else "direct_markethub"
                    ),
                },
                "topology": {
                    "discovery": discovery_backend,
                    "formal_mode": selection.mode,
                    "formal_backends": list(selection.backend_ids),
                },
                "discovery_artifact_hash": discovery_hash,
                "formal_runs": [
                    {
                        "backend_id": item.backend_id,
                        "adapter_version": item.adapter_version,
                        "engine_version": item.engine_version,
                        "status": item.status,
                    }
                    for item in formal_results
                ],
                "result": result_record,
                "evidence": evidence,
            }
            validate_run_manifest(manifest)
            write_json(staging.path / "run_manifest.json", manifest)
            published = staging.publish()
        evidence_index = {
            "schema": "quant-research.evidence-index.v1",
            "run_id": run_id,
            "run_manifest": str((published / "run_manifest.json").resolve()),
            "evidence": evidence,
        }
        write_json(self.layout.evidence / f"{run_id}.json", evidence_index)
        return manifest, (published / "run_manifest.json").resolve()

    def _resolve_discovery(
        self,
        package: StrategyPackage,
        value: Any,
    ) -> str | None:
        policy = package.discovery_policy
        request = value if isinstance(value, dict) else {"mode": "skip"}
        mode = str(request.get("mode", "skip"))
        if policy == "required" and mode != "run":
            raise ValueError("strategy package requires discovery")
        if policy == "forbidden" and mode != "skip":
            raise ValueError("strategy package forbids discovery")
        if mode == "skip":
            return None
        if mode != "run":
            raise ValueError(f"unsupported discovery mode {mode!r}")
        backend = str(request.get("backend", ""))
        implementations = package.implementations("discovery")
        if not backend:
            if len(implementations) != 1:
                raise ValueError(
                    "discovery backend must be explicit when implementation is not unique"
                )
            backend = next(iter(implementations))
        if backend not in implementations:
            raise ValueError(f"strategy package has no discovery implementation for {backend!r}")
        self.registry.profile("discovery", backend)
        return backend

    def _run_formal(
        self,
        backend_id: str,
        package: StrategyPackage,
        parameters: dict[str, Any],
        snapshot: ResolvedSnapshot,
        output: Path,
        config: dict[str, Any],
        run_id: str,
        cache_policy: str,
    ) -> FormalAdapterResult:
        adapter = self.registry.create("formal", backend_id)
        with self.data_adapter.cache(
            policy=cache_policy,
            snapshot=snapshot,
            layout=self.layout,
            consumer=backend_id,
            run_id=run_id,
            evidence_root=output,
        ) as cache:
            return adapter.run(
                FormalRunInput(
                    package,
                    parameters,
                    snapshot,
                    output,
                    config,
                    cache.path,
                    cache.policy,
                    cache.transform_version,
                )
            )


def validate_package(
    path: Path,
    parameters: dict[str, Any] | None = None,
) -> StrategyPackage:
    package = load_package(path)
    package.resolve_parameters(parameters)
    return package


def resolve_snapshot(
    value: dict[str, Any],
    runtime_root: Path = Path(".runtime"),
) -> ResolvedSnapshot:
    return StrategyWorkspace(runtime_root).resolve_snapshot(value)


def run(
    request: dict[str, Any],
    runtime_root: Path = Path(".runtime"),
    *,
    request_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    return StrategyWorkspace(runtime_root).run(request, request_root=request_root)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _profile_identity(profile: Any) -> dict[str, Any]:
    return {
        "backend_id": profile.backend_id,
        "role": profile.role,
        "adapter_version": profile.adapter_version,
        "engine_version": profile.engine_version,
        "provides": sorted(profile.provides),
        "conditional": dict(sorted(profile.conditional.items())),
    }
