from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from quant_runtime.adapters.data.markethub import (
    AdapterStorage,
    MarketHubDataAdapter,
    ResolvedSnapshot,
)
from quant_runtime.adapters.discovery.qlib.capsule import (
    build_discovery_capsule,
    capsule_bytes,
)
from quant_runtime.adapters.formal.nautilus.reporting_input import REPORTING_INPUT_SCHEMA
from quant_runtime.adapters.interface import (
    DiscoveryAdapterResult,
    DiscoveryRunInput,
    FormalAdapterResult,
    FormalRunInput,
)
from quant_runtime.artifacts import canonical_json, sha256_bytes, sha256_value, write_json
from quant_runtime.capabilities import AdapterRegistry, ExecutionPlan, FormalExecution
from quant_runtime.comparison import compare_results
from quant_runtime.materialization import VerifiedPackageMaterializer
from quant_runtime.package import StrategyPackage
from quant_runtime.registry import production_registry
from quant_runtime.sandbox import SandboxRunner
from quant_runtime.sandbox.oci import production_backend
from quant_runtime.sandbox.outcome import workspace_run_error

WORKER_ID = "quant-runtime/0.2.3"


class SandboxAttemptFailure(RuntimeError):
    def __init__(self, outcome: Mapping[str, Any]) -> None:
        super().__init__("sandbox attempt failed")
        self.outcome = dict(outcome)


class WorkspaceClientPort(Protocol):
    def get_run(self, run_id: str) -> dict[str, Any]: ...
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Path) -> dict[str, Any]: ...
    def publish_record(
        self, record: Mapping[str, Any], *, artifacts: tuple[Mapping[str, Any], ...] = ()
    ) -> dict[str, Any]: ...


class WorkspaceWorkerPort(Protocol):
    def start_attempt(self, run_id: str, *, worker_id: str) -> dict[str, Any]: ...
    def bind_run_identity(self, run_id: str, identity: Mapping[str, Any]) -> dict[str, Any]: ...
    def complete_attempt(
        self,
        run_id: str,
        result: Mapping[str, Any],
        *,
        artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> dict[str, Any]: ...
    def reject_attempt(
        self,
        run_id: str,
        result: Mapping[str, Any],
        *,
        artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> dict[str, Any]: ...
    def fail_attempt(self, run_id: str, error: Mapping[str, Any]) -> dict[str, Any]: ...


class RuntimeExecutor:
    """Execute one immutable Workspace request and atomically close its current attempt."""

    def __init__(
        self,
        client: WorkspaceClientPort,
        worker: WorkspaceWorkerPort,
        *,
        registry: AdapterRegistry | None = None,
        data_adapter: MarketHubDataAdapter | None = None,
        worker_id: str = WORKER_ID,
        sandbox_backend: Any | None = None,
    ) -> None:
        self.client = client
        self.worker = worker
        self.registry = registry or production_registry()
        self.data_adapter = data_adapter or MarketHubDataAdapter()
        self.worker_id = worker_id
        self.sandbox_backend = sandbox_backend
        self.package_materializer = VerifiedPackageMaterializer(client)

    def execute(self, request_id: str) -> dict[str, Any]:
        run = self.client.get_run(request_id)
        if run["run_id"] != request_id:
            raise ValueError("Workspace returned a different canonical request identity")
        if run["status"] in {"completed", "rejected", "failed", "running"}:
            return run

        started = self.worker.start_attempt(request_id, worker_id=self.worker_id)
        attempt_id = str(started["current_attempt_id"])
        try:
            with TemporaryDirectory(prefix=f"quant-runtime-{attempt_id}-") as temporary:
                root = Path(temporary)
                run = self.client.get_run(request_id)
                package = self.package_materializer.materialize(run["package"], root / "package")
                request = _object(run, "request")
                if _object(request, "strategy_package") != package.package_ref:
                    raise ValueError("hydrated package differs from the immutable run request")
                parameters = _object(request, "parameters")
                execution = _object(request, "execution")
                snapshot_manifest = _object(request, "market_snapshot")
                plan = self.registry.resolve_plan(
                    execution,
                    required=package.requirements,
                    discovery_policy=package.discovery_policy,
                    discovery_implementations=package.implementations("discovery"),
                    formal_implementations=package.implementations("formal"),
                )
                storage = AdapterStorage.create(root / "adapter-state")
                snapshot = self.data_adapter.open_snapshot(
                    snapshot_manifest,
                    storage,
                    materialize_artifact=self._materialize_workspace_artifact,
                )
                identity = self._identity(
                    run,
                    package=package,
                    parameters=parameters,
                    snapshot=snapshot,
                    plan=plan,
                )
                self.worker.bind_run_identity(request_id, identity)
                result, artifact_specs = self._run_plan(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    package=package,
                    parameters=parameters,
                    snapshot=snapshot,
                    plan=plan,
                    output=root / "output",
                    storage=storage,
                    request_hash=str(run["request_hash"]),
                    identity=identity,
                    package_record=_object(run, "package"),
                    request=request,
                )
                if result["outcome"] == "rejected":
                    return self.worker.reject_attempt(
                        request_id,
                        result,
                        artifacts=artifact_specs,
                    )
                return self.worker.complete_attempt(
                    request_id,
                    result,
                    artifacts=artifact_specs,
                )
        except SandboxAttemptFailure as exc:
            return self.worker.fail_attempt(request_id, workspace_run_error(exc.outcome))
        except Exception as exc:
            return self.worker.fail_attempt(
                request_id,
                {
                    "schema": "quant-research.run-error.v1",
                    "code": "runtime_execution_failed",
                    "message": str(exc),
                    "retryable": _retryable(exc),
                    "details": {
                        "attempt_id": attempt_id,
                        "exception_type": type(exc).__name__,
                        "request_id": request_id,
                    },
                },
            )

    def _materialize_workspace_artifact(self, uri: str, destination: Path) -> Path:
        self.client.verify_artifact(uri)
        value = self.client.materialize_artifact(uri, destination)
        return Path(str(value["path"]))

    def _identity(
        self,
        run: dict[str, Any],
        *,
        package: StrategyPackage,
        parameters: dict[str, Any],
        snapshot: ResolvedSnapshot,
        plan: ExecutionPlan,
    ) -> dict[str, Any]:
        formal = [
            {
                "formal_id": item.formal_id,
                "adapter": self.registry.profile("formal", item.adapter).identity(),
                "config_hash": sha256_value(item.config),
                "read_semantics": _read_semantics(item),
            }
            for item in plan.formal
        ]
        discovery = (
            {
                "adapter": self.registry.profile(
                    "discovery", str(plan.discovery_adapter)
                ).identity(),
                "config_hash": sha256_value(plan.discovery_config),
            }
            if plan.discovery_adapter is not None
            else None
        )
        return {
            "schema": "quant-runtime.identity.v2",
            "request_id": run["run_id"],
            "request_hash": run["request_hash"],
            "strategy_package": package.package_ref,
            "parameters_hash": package.parameters_hash(parameters),
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_mode": snapshot.mode,
            "snapshot_source": snapshot.manifest["source"],
            "snapshot_query": snapshot.manifest["query"],
            "topology": plan.topology,
            "discovery": discovery,
            "formal": formal,
            "data_adapter": {
                "adapter": self.data_adapter.name,
                "adapter_version": self.data_adapter.adapter_version,
            },
        }

    def _run_plan(
        self,
        *,
        request_id: str,
        attempt_id: str,
        package: StrategyPackage,
        parameters: dict[str, Any],
        snapshot: ResolvedSnapshot,
        plan: ExecutionPlan,
        output: Path,
        storage: AdapterStorage,
        request_hash: str,
        identity: dict[str, Any],
        package_record: dict[str, Any],
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        output.mkdir(parents=True, exist_ok=True)
        discovery_result = None
        discovery_outcome = None
        if plan.discovery_adapter is not None:
            if request.get("schema") == "quant-research.workspace-run-request.v4":
                discovery_result, discovery_outcome = self._run_sandbox_discovery(
                    package_record=package_record,
                    package=package,
                    profile=_object(request, "sandbox_profile"),
                    parameters=parameters,
                    snapshot=snapshot,
                    output=output / "discovery" / plan.discovery_adapter,
                    config=plan.discovery_config,
                )
            else:
                adapter = self.registry.create("discovery", plan.discovery_adapter)
                discovery_result = adapter.run(
                    DiscoveryRunInput(
                        package=package,
                        parameters=parameters,
                        snapshot=snapshot,
                        output=output / "discovery" / plan.discovery_adapter,
                        config=plan.discovery_config,
                    )
                )

        strategy_rejected = (
            discovery_outcome is not None
            and discovery_outcome["classification"] == "strategy_rejection"
        )

        formal_results = (
            ()
            if strategy_rejected
            else tuple(
                self._run_formal(
                    request_id,
                    execution=item,
                    package=package,
                    parameters=parameters,
                    snapshot=snapshot,
                    output=output / "formal" / item.formal_id,
                    storage=storage,
                )
                for item in plan.formal
            )
        )
        comparison = (
            None
            if strategy_rejected
            else compare_results(plan.topology, formal_results, agreement=plan.agreement)
        )
        rejected = strategy_rejected or (
            comparison is not None and comparison.get("status") == "rejected"
        )
        result: dict[str, Any] = {
            "schema": (
                "quant-research.result.v3"
                if discovery_outcome is not None
                else "quant-research.result.v2"
            ),
            "outcome": "rejected" if rejected else "completed",
            "summary": {
                "request_id": request_id,
                "attempt_id": attempt_id,
                "topology": plan.topology,
                "snapshot_id": snapshot.snapshot_id,
                "formal_execution_count": len(formal_results),
                "status": "rejected" if rejected else "completed",
            },
            "formal": {
                item.formal_id: {"adapter": item.backend_id, "metrics": item.metrics}
                for item in formal_results
            },
        }
        if discovery_result is not None:
            result["discovery"] = {
                "adapter": discovery_result.backend_id,
                "metrics": discovery_result.metrics,
            }
        if discovery_outcome is not None:
            result["sandbox"] = discovery_outcome
        if comparison is not None:
            result["comparison"] = comparison
        if rejected:
            result["reason"] = (
                "strategy rejected Qlib discovery"
                if strategy_rejected
                else "agreement gate rejected the formal executions"
            )

        _write_native_evidence_indexes(output)
        manifest = {
            "schema": "quant-research.run-manifest.v2",
            "run_id": request_id,
            "attempt_id": attempt_id,
            "request_hash": request_hash,
            "status": result["outcome"],
            "result": result,
            "artifacts": [],
            "runtime_identity": identity,
        }
        write_json(output / "runtime_manifest.json", manifest)
        specs = tuple(
            _artifact_spec(path, output) for path in sorted(output.rglob("*")) if path.is_file()
        )
        return result, specs

    def _run_sandbox_discovery(
        self,
        *,
        package_record: dict[str, Any],
        package: StrategyPackage,
        profile: dict[str, Any],
        parameters: dict[str, Any],
        snapshot: ResolvedSnapshot,
        output: Path,
        config: dict[str, Any],
    ) -> tuple[DiscoveryAdapterResult | None, dict[str, Any]]:
        capsule = build_discovery_capsule(snapshot)
        publication = self.client.publish_record(
            {
                "record_id": "sandbox-input."
                + sha256_value(
                    {
                        "capsule_id": capsule["capsule_id"],
                        "parameters_hash": sha256_value(parameters),
                    }
                ),
                "record_type": "quant-runtime.sandbox-input.v1",
                "created_at": snapshot.manifest["resolved_at"],
                "payload": {
                    "schema": "quant-runtime.sandbox-input.v1",
                    "snapshot_id": snapshot.snapshot_id,
                    "capsule_id": capsule["capsule_id"],
                    "parameters_hash": sha256_value(parameters),
                },
            },
            artifacts=(
                {
                    "source": capsule_bytes(capsule),
                    "media_type": "application/json",
                    "logical_role": "sandbox-input",
                    "name": "discovery-capsule.json",
                },
                {
                    "source": canonical_json(parameters),
                    "media_type": "application/json",
                    "logical_role": "sandbox-input",
                    "name": "parameters.json",
                },
            ),
        )
        inputs = {item["name"]: item for item in publication["artifacts"]}
        runner = SandboxRunner(
            self.client,
            backend=self.sandbox_backend or production_backend(),
        )
        value = runner.invoke(
            package_record=package_record,
            profile=profile,
            phase="discovery",
            parameters=parameters,
            input_artifacts=inputs,
            phase_config={
                "adapter": "qlib",
                "config": config,
                "entrypoint": package.resolve_entrypoint("discovery", "qlib"),
                "snapshot_id": snapshot.snapshot_id,
            },
            output_destination=output,
        )
        outcome = _object(value, "sandbox")
        if value["classification"] == "strategy_rejection":
            return None, outcome
        if value["classification"] != "success":
            raise SandboxAttemptFailure(outcome)
        payload = _object(value, "payload")
        metrics = _object(payload, "metrics")
        return (
            DiscoveryAdapterResult(
                backend_id=str(payload["backend_id"]),
                adapter_version=str(payload["adapter_version"]),
                engine_version=str(payload["engine_version"]),
                artifact_hash=str(payload["artifact_hash"]),
                metrics=metrics,
                evidence=tuple(payload.get("evidence", ())),
            ),
            outcome,
        )

    def _run_formal(
        self,
        request_id: str,
        *,
        execution: FormalExecution,
        package: StrategyPackage,
        parameters: dict[str, Any],
        snapshot: ResolvedSnapshot,
        output: Path,
        storage: AdapterStorage,
    ) -> FormalAdapterResult:
        adapter = self.registry.create("formal", execution.adapter)
        semantics = _read_semantics(execution)
        with self.data_adapter.cache(
            policy=str(semantics["local_cache"]),
            snapshot=snapshot,
            layout=storage,
            consumer=execution.formal_id,
            run_id=request_id,
            evidence_root=output,
        ) as cache:
            return adapter.run(
                FormalRunInput(
                    package=package,
                    parameters=parameters,
                    snapshot=snapshot,
                    output=output,
                    config=execution.config,
                    cache_path=cache.path,
                    cache_policy=cache.policy,
                    cache_transform_version=cache.transform_version,
                ),
                formal_id=execution.formal_id,
            )


def _object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(item)


def _read_semantics(execution: FormalExecution) -> dict[str, Any]:
    raw = execution.config.get("market_data", {})
    if not isinstance(raw, Mapping):
        raise ValueError("formal config market_data must be an object")
    local_cache = str(raw.get("local_cache", "none"))
    if local_cache == "persistent":
        raise ValueError(
            "persistent cache requires a Strategy Workspace ArtifactRef and is not supported"
        )
    if local_cache not in {"none", "ephemeral"}:
        raise ValueError("formal market_data.local_cache is invalid")
    return {
        "local_cache": local_cache,
        "method": "non_authoritative_cache" if local_cache != "none" else "snapshot_native",
    }


def _artifact_spec(path: Path, root: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    if relative == "runtime_manifest.json":
        role = "runtime-manifest"
        record_schema = "quant-research.run-manifest.v2"
    elif relative.startswith("discovery/"):
        role = "discovery-evidence"
        record_schema = None
    elif relative.startswith("formal/"):
        role = "engine-native-evidence"
        record_schema = (
            REPORTING_INPUT_SCHEMA if relative.endswith("/native_statistics.json") else None
        )
    else:
        role = "runtime-evidence"
        record_schema = None
    return {
        "source": path,
        "logical_role": role,
        "record_schema": record_schema,
        "name": relative,
    }


def _write_native_evidence_indexes(output: Path) -> None:
    formal_root = output / "formal"
    if not formal_root.is_dir():
        return
    for execution in sorted(path for path in formal_root.iterdir() if path.is_dir()):
        files = sorted(
            path
            for path in execution.rglob("*")
            if path.is_file() and path.name != "evidence_index.json"
        )
        write_json(
            execution / "evidence_index.json",
            {
                "schema": "quant-runtime.native-evidence-index.v1",
                "formal_id": execution.name,
                "files": [
                    {
                        "path": path.relative_to(execution).as_posix(),
                        "sha256": sha256_bytes(path.read_bytes()),
                        "bytes": path.stat().st_size,
                    }
                    for path in files
                ],
            },
        )


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, ConnectionError | TimeoutError | OSError) and not isinstance(
        exc, FileNotFoundError | PermissionError
    )
