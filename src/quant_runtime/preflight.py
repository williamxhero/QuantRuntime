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
            package_record = self.client.get_registered_package(value["strategy_package"])
            if value["schema"] in {
                "quant-research.runtime-preflight-request.v2",
                "quant-research.runtime-preflight-request.v3",
            }:
                resolved = self.policy_registry.resolve(package_record, value["sandbox_profile"])
                _verify_conformance(self.client, package_record, value, resolved.identity_hash)
            snapshot_value = _snapshot_request(value["snapshot_request"])
            request = SnapshotRequest.from_dict(snapshot_value)
            required_semantics = _required_semantics(snapshot_value)
            as_of = _as_of(snapshot_value)
            with TemporaryDirectory(prefix="quant-runtime-preflight-") as temporary:
                package = VerifiedPackageMaterializer(self.client).materialize(
                    package_record,
                    Path(temporary) / "package",
                )
                if package.frequencies and request.frequency not in package.frequencies:
                    raise PreflightRequestError(
                        "strategy package does not support MarketHub frequency "
                        f"{request.frequency!r}"
                    )
                self.registry.resolve_plan(
                    value["execution"],
                    required=package.requirements,
                    discovery_policy=package.discovery_policy,
                    discovery_implementations=package.implementations("discovery"),
                    formal_implementations=package.implementations("formal"),
                )
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
