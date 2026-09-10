from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from strategy_workspace import WorkspaceError

from quant_runtime.artifacts import canonical_json, sha256_bytes, sha256_value
from quant_runtime.sandbox import SandboxRunner
from quant_runtime.sandbox.invocation import SandboxBackend
from quant_runtime.sandbox.oci import production_backend

DIMENSIONS = frozenset(
    {
        "decision_time",
        "warm_up",
        "strict_comparison",
        "entry",
        "exit",
        "sizing",
        "state_transition",
        "add_reduce",
    }
)


class WorkspaceConformancePort(Protocol):
    def get_registered_package(self, package_ref: Mapping[str, Any]) -> dict[str, Any]: ...
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Any) -> dict[str, Any]: ...
    def publish_record(
        self, record: Mapping[str, Any], *, artifacts: tuple[Mapping[str, Any], ...] = ()
    ) -> dict[str, Any]: ...
    def get_record(self, record_id: str) -> dict[str, Any]: ...


class ConformanceRequestError(ValueError):
    pass


class RuntimeConformance:
    """Observe a package against frozen synthetic fixtures before live preflight."""

    def __init__(
        self, client: WorkspaceConformancePort, *, backend: SandboxBackend | None = None
    ) -> None:
        self._client = client
        selected_backend = backend or production_backend()
        self._production = selected_backend.production
        self._runner = SandboxRunner(client, backend=selected_backend)

    def conform(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = _request(request)
            package_record = self._client.get_registered_package(value["strategy_package"])
            scenarios = {
                f"scenario-{index:04d}.json": artifact
                for index, artifact in enumerate(value["behavioral_scenarios"])
            }
            inputs = scenarios
            phase_config = None
            if self._production:
                inputs = {
                    **scenarios,
                    "parameters.json": self._parameters_artifact(
                        package_record, value["parameters"]
                    ),
                }
                phase_config = {
                    "adapter": "runtime",
                    "entrypoint": _conformance_entrypoint(package_record),
                }
            outcome = self._runner.invoke(
                package_record=package_record,
                profile=value["sandbox_profile"],
                phase="behavioral_conformance",
                parameters=value["parameters"],
                input_artifacts=inputs,
                phase_config=phase_config,
            )
            if outcome["classification"] != "success":
                return _rejected(outcome["classification"], "sandbox invocation did not succeed")
            payload = _worker_payload(outcome["payload"])
            if payload["status"] == "rejected":
                return _rejected("strategy_rejection", "strategy failed behavioral conformance")
            diagnostics = outcome.get("diagnostics")
            if diagnostics is None:
                diagnostics = outcome.get("sandbox", {}).get("diagnostics", {})
            return self._publish(value, package_record, payload, diagnostics)
        except ConformanceRequestError:
            return _rejected("policy_rejection", "behavioral conformance request rejected")
        except Exception:
            return _rejected("engine_failure", "behavioral conformance execution failed")

    def _parameters_artifact(
        self, package_record: Mapping[str, Any], parameters: Mapping[str, Any]
    ) -> dict[str, Any]:
        package_hash = str(package_record["package_ref"]["package_hash"])
        parameters_hash = sha256_value(dict(parameters))
        record_id = "sandbox-input." + sha256_value(
            {
                "phase": "behavioral_conformance",
                "package_hash": package_hash,
                "parameters_hash": parameters_hash,
            }
        )
        payload = {
            "schema": "quant-runtime.sandbox-input.v1",
            "phase": "behavioral_conformance",
            "package_hash": package_hash,
            "parameters_hash": parameters_hash,
        }
        try:
            publication = self._client.get_record(record_id)
            if publication.get("payload") != payload:
                raise ConformanceRequestError("conformance input publication identity conflict")
        except WorkspaceError as exc:
            if exc.code != "record_not_found":
                raise
            publication = self._client.publish_record(
                {
                    "record_id": record_id,
                    "record_type": "quant-runtime.sandbox-input.v1",
                    "payload": payload,
                },
                artifacts=(
                    {
                        "source": canonical_json(dict(parameters)),
                        "media_type": "application/json",
                        "logical_role": "sandbox-input",
                        "name": "parameters.json",
                    },
                ),
            )
        artifacts = publication.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise ConformanceRequestError("conformance parameters artifact is unavailable")
        artifact = dict(artifacts[0])
        if artifact.get("sha256") != sha256_bytes(canonical_json(dict(parameters))):
            raise ConformanceRequestError("conformance parameters artifact identity mismatch")
        return artifact

    def _publish(
        self,
        request: dict[str, Any],
        package_record: Mapping[str, Any],
        payload: dict[str, Any],
        diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        package_hash = str(package_record["package_ref"]["package_hash"])
        profile_hash = sha256_value(request["sandbox_profile"])
        parameters_hash = sha256_value(request["parameters"])
        scenario_hash = sha256_value(
            [
                {"sha256": item["sha256"], "bytes": item["bytes"]}
                for item in request["behavioral_scenarios"]
            ]
        )
        evidence = {
            "schema": "quant-runtime.behavioral-conformance-evidence.v1",
            "evidence_level": "behavioral-conformance",
            "package_hash": package_hash,
            "parameters_hash": parameters_hash,
            "profile_hash": profile_hash,
            "scenario_hash": scenario_hash,
            "dimensions": payload["dimensions"],
            "trace": payload["trace"],
            "diagnostics": dict(diagnostics),
        }
        digest = sha256_value(evidence)
        record_id = "sha256:" + digest
        try:
            publication = self._client.get_record(record_id)
            if publication.get("payload") != evidence or len(publication.get("artifacts", [])) != 1:
                raise ConformanceRequestError("conformance publication identity conflict")
        except WorkspaceError as exc:
            if exc.code != "record_not_found":
                raise
            publication = self._client.publish_record(
                {
                    "record_id": record_id,
                    "record_type": "quant-runtime.behavioral-conformance.v1",
                    "payload": evidence,
                    "lineage": [
                        {
                            "source_kind": "strategy_package",
                            "source_id": package_hash,
                            "relation": "behaviorally-conforms",
                        }
                    ],
                },
                artifacts=(
                    {
                        "source": canonical_json(evidence),
                        "media_type": "application/json",
                        "record_schema": "quant-runtime.behavioral-conformance-evidence.v1",
                        "logical_role": "behavioral-conformance",
                        "name": "behavioral-conformance.json",
                    },
                ),
            )
        artifact = publication["artifacts"][0]
        if artifact["sha256"] != sha256_bytes(canonical_json(evidence)):
            raise ConformanceRequestError("conformance artifact identity mismatch")
        reference = {
            "schema": "quant-runtime.behavioral-conformance-ref.v1",
            "conformance_id": record_id,
            "status": "passed",
            "evidence_level": "behavioral-conformance",
            "package_hash": package_hash,
            "parameters_hash": parameters_hash,
            "profile_hash": profile_hash,
            "scenario_hash": scenario_hash,
            "artifact": artifact,
        }
        return {
            "schema": "quant-research.runtime-conformance-result.v1",
            "status": "accepted",
            "behavioral_conformance": reference,
        }


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = {str(key): item for key, item in value.items()}
    required = {
        "schema",
        "strategy_package",
        "parameters",
        "sandbox_profile",
        "behavioral_scenarios",
    }
    if (
        set(request) != required
        or request.get("schema") != "quant-research.runtime-conformance-request.v1"
    ):
        raise ConformanceRequestError("conformance request shape is invalid")
    for key in ("strategy_package", "parameters", "sandbox_profile"):
        if not isinstance(request[key], Mapping):
            raise ConformanceRequestError(f"conformance request {key} must be an object")
        request[key] = dict(request[key])
    scenarios = request["behavioral_scenarios"]
    if (
        not isinstance(scenarios, list)
        or not scenarios
        or not all(isinstance(item, Mapping) for item in scenarios)
    ):
        raise ConformanceRequestError("behavioral scenarios must be a non-empty ArtifactRef array")
    request["behavioral_scenarios"] = [dict(item) for item in scenarios]
    return request


def _worker_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = {str(key): item for key, item in value.items()}
    if (
        set(payload) != {"schema", "status", "dimensions", "trace"}
        or payload.get("schema") != "quant-runtime.behavioral-conformance.v1"
    ):
        raise ConformanceRequestError("behavioral conformance worker payload shape is invalid")
    if payload["status"] not in {"passed", "rejected"}:
        raise ConformanceRequestError("behavioral conformance status is invalid")
    if not isinstance(payload["dimensions"], Mapping) or set(payload["dimensions"]) != DIMENSIONS:
        raise ConformanceRequestError("behavioral conformance dimensions are incomplete")
    if (
        not isinstance(payload["trace"], list)
        or len(payload["trace"]) > 10_000
        or not all(isinstance(item, Mapping) for item in payload["trace"])
    ):
        raise ConformanceRequestError("behavioral conformance trace is invalid")
    return {
        **payload,
        "dimensions": {str(key): dict(item) for key, item in payload["dimensions"].items()},
        "trace": [dict(item) for item in payload["trace"]],
    }


def _conformance_entrypoint(package_record: Mapping[str, Any]) -> str:
    manifest = package_record.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ConformanceRequestError("registered package manifest is unavailable")
    implementations = manifest.get("implementations")
    if not isinstance(implementations, Mapping):
        raise ConformanceRequestError("registered package implementations are unavailable")
    conformance = implementations.get("conformance")
    if not isinstance(conformance, Mapping) or set(conformance) != {"runtime"}:
        raise ConformanceRequestError("registered package lacks the Runtime conformance interface")
    entrypoint = conformance.get("runtime")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ConformanceRequestError("registered package conformance entrypoint is invalid")
    return entrypoint


def _rejected(classification: str, message: str) -> dict[str, Any]:
    return {
        "schema": "quant-research.runtime-conformance-result.v1",
        "status": "rejected",
        "observation": {
            "classification": classification,
            "code": "behavioral_conformance_failed",
            "message": message,
        },
    }
