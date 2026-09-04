from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from quant_runtime.adapters.data.markethub import (
    MarketHubContractError,
    MarketHubDataAdapter,
    SnapshotRequest,
)
from quant_runtime.adapters.data.markethub.contract import validate_snapshot_manifest
from quant_runtime.artifacts import sha256_value
from quant_runtime.capabilities import AdapterRegistry
from quant_runtime.materialization import VerifiedPackageMaterializer
from quant_runtime.registry import production_registry
from quant_runtime.sandbox.policy import SandboxPolicyRegistry


class WorkspacePreflightClientPort(Protocol):
    def get_registered_package(self, package_ref: Mapping[str, Any]) -> dict[str, Any]: ...
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Path) -> dict[str, Any]: ...
    def get_record(self, record_id: str) -> dict[str, Any]: ...


class PreflightRequestError(ValueError):
    pass


class RuntimePreflight:
    """Freeze one exact MarketHub request behind the public Runtime preflight interface."""

    def __init__(
        self,
        client: WorkspacePreflightClientPort,
        *,
        registry: AdapterRegistry | None = None,
        data_adapter: MarketHubDataAdapter | None = None,
        policy_registry: SandboxPolicyRegistry | None = None,
    ) -> None:
        self.client = client
        self.registry = registry or production_registry()
        self.data_adapter = data_adapter or MarketHubDataAdapter()
        self.policy_registry = policy_registry or SandboxPolicyRegistry()

    def preflight(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = _draft(draft)
            snapshot_value = _snapshot_request(value["snapshot_request"])
            request = SnapshotRequest.from_dict(snapshot_value)
            _validate_local_request(
                self.client, self.registry, self.policy_registry, value, request
            )
            required_semantics = _required_semantics(snapshot_value)
            as_of = _as_of(snapshot_value)
            frozen_snapshot = self.data_adapter.freeze_reference(
                request,
                as_of=as_of,
                required_semantics=required_semantics,
            )
            return {
                "schema": "quant-research.runtime-preflight-result.v1",
                "status": "accepted",
                "frozen_snapshot": frozen_snapshot,
                "evidence": {
                    "strategy_package": value["strategy_package"],
                    "verification": frozen_snapshot["verification"],
                    "data_semantics": frozen_snapshot["data_semantics"],
                    **(
                        {"behavioral_conformance": value["behavioral_conformance"]}
                        if value["schema"]
                        in {
                            "quant-research.runtime-preflight-request.v2",
                            "quant-research.runtime-preflight-request.v3",
                        }
                        else {}
                    ),
                },
            }
        except PreflightRequestError as exc:
            return _failure("request_invalid", "preflight_request_invalid", str(exc))
        except MarketHubContractError as exc:
            message = str(exc)
            if message.startswith("required data semantic"):
                return _failure("request_invalid", "data_semantics_unavailable", message)
            classification = "valid_absence" if "no rows" in message else "market_data_incident"
            return _failure(classification, "markethub_preflight_failed", message)
        except Exception as exc:
            return _failure("request_invalid", "preflight_validation_failed", str(exc))


def validate_frozen_preflight(
    client: WorkspacePreflightClientPort,
    draft: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    registry: AdapterRegistry | None = None,
    policy_registry: SandboxPolicyRegistry | None = None,
) -> None:
    value, request = validate_frozen_transport(draft, result)
    _validate_local_request(
        client,
        registry or production_registry(),
        policy_registry or SandboxPolicyRegistry(),
        value,
        request,
    )


def validate_frozen_transport(
    draft: Mapping[str, Any], result: Mapping[str, Any]
) -> tuple[dict[str, Any], SnapshotRequest]:
    """Validate frozen transport integrity without touching any external owner."""

    value = _draft(draft)
    expected_result_fields = {"schema", "status", "frozen_snapshot", "evidence"}
    if (
        set(result) != expected_result_fields
        or result.get("schema") != "quant-research.runtime-preflight-result.v1"
        or result.get("status") != "accepted"
    ):
        raise PreflightRequestError("frozen preflight result is invalid")
    snapshot = result.get("frozen_snapshot")
    evidence = result.get("evidence")
    if not isinstance(snapshot, Mapping) or not isinstance(evidence, Mapping):
        raise PreflightRequestError("frozen preflight content is invalid")
    snapshot_value = {str(key): item for key, item in snapshot.items()}
    validate_snapshot_manifest(snapshot_value)
    _validate_frozen_snapshot_shape(snapshot_value)
    snapshot_identity = {
        "schema": "strategy-workspace.market-snapshot-request.v1",
        "source": snapshot.get("source"),
        "query": snapshot.get("query"),
        "calendar": snapshot.get("calendar"),
        "contract_mapping": snapshot.get("contract_mapping"),
        "trust_policy": snapshot.get("trust_policy"),
        "as_of": snapshot.get("as_of"),
        "required_semantics": snapshot.get("required_semantics"),
        "data_semantics": snapshot.get("data_semantics"),
        "verification": snapshot.get("verification"),
    }
    if snapshot.get("snapshot_id") != f"sha256:{sha256_value(snapshot_identity)}":
        raise PreflightRequestError("frozen snapshot identity is invalid")
    request_value = _snapshot_request(value["snapshot_request"])
    request = SnapshotRequest.from_dict(request_value)
    source = snapshot.get("source")
    query = snapshot.get("query")
    if not isinstance(source, Mapping) or not isinstance(query, Mapping):
        raise PreflightRequestError("frozen snapshot source or query is invalid")
    request_identity = request.identity_payload()
    expected_source = request_identity["source"]
    expected_query = request_identity["query"]
    if (
        set(source) != set(expected_source) | {"adapter_version", "data_revision"}
        or any(source.get(key) != item for key, item in expected_source.items())
        or source.get("adapter_version") != MarketHubDataAdapter.adapter_version
        or not isinstance(source.get("data_revision"), str)
        or not source.get("data_revision")
        or dict(query) != expected_query
        or snapshot.get("calendar") != request.calendar
        or snapshot.get("contract_mapping") != request.contract_mapping
        or snapshot.get("as_of") != _as_of(request_value)
        or snapshot.get("required_semantics") != list(_required_semantics(request_value))
    ):
        raise PreflightRequestError("frozen snapshot does not match the request")
    required_evidence = {
        "strategy_package",
        "verification",
        "data_semantics",
        "behavioral_conformance",
    }
    if set(evidence) != required_evidence:
        raise PreflightRequestError("frozen preflight evidence fields are invalid")
    if (
        evidence.get("strategy_package") != value["strategy_package"]
        or evidence.get("verification") != snapshot.get("verification")
        or evidence.get("data_semantics") != snapshot.get("data_semantics")
        or evidence.get("behavioral_conformance") != value["behavioral_conformance"]
    ):
        raise PreflightRequestError("frozen preflight evidence does not match the request")
    return value, request


def _validate_frozen_snapshot_shape(snapshot: Mapping[str, Any]) -> None:
    expected = {
        "schema",
        "snapshot_id",
        "mode",
        "trust_policy",
        "source",
        "query",
        "calendar",
        "contract_mapping",
        "as_of",
        "required_semantics",
        "data_semantics",
        "verification",
        "resolved_at",
    }
    if (
        set(snapshot) != expected
        or snapshot.get("schema") != "quant-research.market-snapshot-ref.v2"
        or snapshot.get("mode") != "reference"
        or snapshot.get("trust_policy") != "verified_immutable"
    ):
        raise PreflightRequestError("frozen snapshot fields are invalid")
    source = snapshot.get("source")
    query = snapshot.get("query")
    semantics = snapshot.get("data_semantics")
    verification = snapshot.get("verification")
    if not all(isinstance(value, Mapping) for value in (source, query, semantics, verification)):
        raise PreflightRequestError("frozen snapshot objects are invalid")
    assert isinstance(source, Mapping)
    assert isinstance(query, Mapping)
    assert isinstance(semantics, Mapping)
    assert isinstance(verification, Mapping)
    source_fields = {"adapter", "adapter_version", "endpoint_contract", "base_url", "data_revision"}
    if "partial_publication" in source:
        source_fields.add("partial_publication")
    if set(source) != source_fields:
        raise PreflightRequestError("frozen snapshot source fields are invalid")
    if set(query) != {"instruments", "start", "end", "frequency", "adjustment"}:
        raise PreflightRequestError("frozen snapshot query fields are invalid")
    semantic_names = {"field_availability", "point_in_time", "time", "provider_lineage"}
    if set(semantics) != semantic_names:
        raise PreflightRequestError("frozen snapshot data semantics are invalid")
    for observation in semantics.values():
        if (
            not isinstance(observation, Mapping)
            or set(observation) != {"status", "reason"}
            or observation.get("status") not in {"verified", "not_evaluated"}
            or not isinstance(observation.get("reason"), str)
        ):
            raise PreflightRequestError("frozen snapshot data semantics are invalid")
    verification_fields = {
        "canonical_input_hash",
        "data_version",
        "dataset_version",
        "catalog_hash",
        "calendar_hash",
        "coverage_hash",
    }
    if set(verification) != verification_fields:
        raise PreflightRequestError("frozen snapshot verification fields are invalid")
    for name in ("canonical_input_hash", "catalog_hash", "calendar_hash", "coverage_hash"):
        identity = verification.get(name)
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
        ):
            raise PreflightRequestError("frozen snapshot verification identity is invalid")
    if any(
        not isinstance(verification.get(name), str) or not verification[name]
        for name in ("data_version", "dataset_version")
    ):
        raise PreflightRequestError("frozen snapshot verification version is invalid")
    resolved_at = snapshot.get("resolved_at")
    if not isinstance(resolved_at, str) or not resolved_at.endswith("Z"):
        raise PreflightRequestError("frozen snapshot resolution time is invalid")
    try:
        datetime.fromisoformat(resolved_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise PreflightRequestError("frozen snapshot resolution time is invalid") from exc


def _validate_local_request(
    client: WorkspacePreflightClientPort,
    registry: AdapterRegistry,
    policy_registry: SandboxPolicyRegistry,
    value: Mapping[str, Any],
    request: SnapshotRequest,
) -> dict[str, Any]:
    package_record = client.get_registered_package(value["strategy_package"])
    if value["schema"] in {
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
    }:
        resolved = policy_registry.resolve(package_record, value["sandbox_profile"])
        _verify_conformance(client, package_record, value, resolved.identity_hash)
    _required_semantics(value["snapshot_request"])
    _as_of(value["snapshot_request"])
    with TemporaryDirectory(prefix="quant-runtime-preflight-") as temporary:
        package = VerifiedPackageMaterializer(client).materialize(
            package_record,
            Path(temporary) / "package",
        )
        if package.frequencies and request.frequency not in package.frequencies:
            raise PreflightRequestError(
                f"strategy package does not support MarketHub frequency {request.frequency!r}"
            )
        registry.resolve_plan(
            value["execution"],
            required=package.requirements,
            discovery_policy=package.discovery_policy,
            discovery_implementations=package.implementations("discovery"),
            formal_implementations=package.implementations("formal"),
        )
    return package_record


def _draft(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = dict(value)
    base = {
        "schema",
        "strategy_package",
        "snapshot_request",
        "parameters",
        "execution",
    }
    sandboxed = {"sandbox_profile", "behavioral_conformance"}
    if set(draft) not in {frozenset(base), frozenset(base | sandboxed)}:
        raise PreflightRequestError("preflight draft has unsupported or missing fields")
    if draft["schema"] not in {
        "quant-research.runtime-preflight-request.v1",
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
    }:
        raise PreflightRequestError("preflight draft schema is invalid")
    if draft["schema"].endswith(".v1") and set(draft) != base:
        raise PreflightRequestError("legacy preflight draft cannot carry sandbox fields")
    if (
        draft["schema"]
        in {
            "quant-research.runtime-preflight-request.v2",
            "quant-research.runtime-preflight-request.v3",
        }
        and set(draft) != base | sandboxed
    ):
        raise PreflightRequestError("sandboxed preflight draft lacks conformance fields")
    if not isinstance(draft["strategy_package"], Mapping):
        raise PreflightRequestError("preflight draft strategy_package must be an object")
    if not isinstance(draft["snapshot_request"], Mapping):
        raise PreflightRequestError("preflight draft snapshot_request must be an object")
    if not isinstance(draft["parameters"], Mapping) or not isinstance(draft["execution"], Mapping):
        raise PreflightRequestError("preflight draft parameters and execution must be objects")
    normalized = {
        **draft,
        "strategy_package": dict(draft["strategy_package"]),
        "snapshot_request": dict(draft["snapshot_request"]),
        "parameters": dict(draft["parameters"]),
        "execution": dict(draft["execution"]),
    }
    if draft["schema"] in {
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
    }:
        if not isinstance(draft["sandbox_profile"], Mapping) or not isinstance(
            draft["behavioral_conformance"], Mapping
        ):
            raise PreflightRequestError("sandbox profile and conformance must be objects")
        normalized["sandbox_profile"] = dict(draft["sandbox_profile"])
        normalized["behavioral_conformance"] = dict(draft["behavioral_conformance"])
    return normalized


def _verify_conformance(
    client: WorkspacePreflightClientPort,
    package_record: Mapping[str, Any],
    draft: Mapping[str, Any],
    profile_hash: str,
) -> None:
    reference = dict(draft["behavioral_conformance"])
    required = {
        "schema",
        "conformance_id",
        "status",
        "evidence_level",
        "package_hash",
        "parameters_hash",
        "profile_hash",
        "scenario_hash",
        "artifact",
    }
    if (
        set(reference) != required
        or reference.get("schema") != "quant-runtime.behavioral-conformance-ref.v1"
    ):
        raise PreflightRequestError("behavioral conformance reference shape is invalid")
    if (
        reference.get("status") != "passed"
        or reference.get("evidence_level") != "behavioral-conformance"
    ):
        raise PreflightRequestError("behavioral conformance did not pass")
    expected = {
        "package_hash": package_record["package_ref"]["package_hash"],
        "parameters_hash": sha256_value(dict(draft["parameters"])),
        "profile_hash": profile_hash,
    }
    if any(reference.get(key) != item for key, item in expected.items()):
        raise PreflightRequestError("behavioral conformance identity does not match the run")
    artifact = reference.get("artifact")
    if not isinstance(artifact, Mapping):
        raise PreflightRequestError("behavioral conformance artifact is invalid")
    verification = client.verify_artifact(str(artifact.get("uri", "")))
    verified = verification.get("artifact", {})
    if verification.get("verified") is not True or any(
        verified.get(key) != artifact.get(key) for key in ("uri", "sha256", "bytes")
    ):
        raise PreflightRequestError("behavioral conformance artifact verification failed")
    publication = client.get_record(str(reference["conformance_id"]))
    evidence = publication.get("payload", {})
    if publication.get(
        "record_type"
    ) != "quant-runtime.behavioral-conformance.v1" or publication.get("artifacts") != [artifact]:
        raise PreflightRequestError(
            "behavioral conformance publication does not match its reference"
        )
    if any(evidence.get(key) != reference.get(key) for key in (*expected, "scenario_hash")) or (
        evidence.get("evidence_level") != "behavioral-conformance"
    ):
        raise PreflightRequestError("behavioral conformance evidence identity is invalid")


def _snapshot_request(value: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = dict(value)
    required = {
        "adapter",
        "snapshot_mode",
        "trust_policy",
        "local_cache",
        "endpoint_contract",
        "base_url",
        "as_of",
        "required_semantics",
        "query",
    }
    optional = {"partial_publication"}
    if not required <= snapshot.keys() or set(snapshot) - required - optional:
        raise PreflightRequestError("snapshot request has unsupported or missing fields")
    if not isinstance(snapshot["query"], Mapping):
        raise PreflightRequestError("snapshot request query must be an object")
    return snapshot


def _required_semantics(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    raw = snapshot["required_semantics"]
    if not isinstance(raw, list):
        raise PreflightRequestError("required_semantics must be an array")
    allowed = {"field_availability", "point_in_time", "time", "provider_lineage"}
    values = tuple(sorted(str(item) for item in raw))
    if len(values) != len(set(values)) or not set(values) <= allowed:
        raise PreflightRequestError("required_semantics contains an unsupported value")
    return values


def _as_of(snapshot: Mapping[str, Any]) -> str:
    value = str(snapshot["as_of"])
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightRequestError("snapshot request as_of must be RFC 3339") from exc
    if parsed.tzinfo is None:
        raise PreflightRequestError("snapshot request as_of must include an offset")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _failure(classification: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema": "quant-research.runtime-preflight-result.v1",
        "status": "failed",
        "observation": {
            "classification": classification,
            "code": code,
            "message": message,
        },
    }
