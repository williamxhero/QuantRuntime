from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from strategy_workspace import WorkspaceError

from quant_runtime.adapters.data.markethub import (
    MarketHubContractError,
    MarketHubDataAdapter,
    SnapshotRequest,
)
from quant_runtime.adapters.data.markethub.contract import validate_snapshot_manifest
from quant_runtime.artifacts import canonical_json, sha256_bytes, sha256_value
from quant_runtime.capabilities import AdapterRegistry
from quant_runtime.conformance import DIMENSIONS
from quant_runtime.materialization import VerifiedPackageMaterializer
from quant_runtime.package import SignalSeriesUnavailable, StrategyPackage
from quant_runtime.registry import production_registry
from quant_runtime.sandbox.policy import SandboxPolicyRegistry


class WorkspacePreflightClientPort(Protocol):
    def get_registered_package(self, package_ref: Mapping[str, Any]) -> dict[str, Any]: ...
    def validate_parameters(
        self, package_ref: Mapping[str, Any], parameters: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Path) -> dict[str, Any]: ...
    def get_record(self, record_id: str) -> dict[str, Any]: ...


class PreflightRequestError(ValueError):
    pass


class PriceLimitConformanceRequired(PreflightRequestError):
    """A new price-limit package cannot be admitted without conformance evidence."""


HISTORICAL_PRICE_LIMIT_PACKAGE_REF = {
    "schema": "quant-research.strategy-package-ref.v1",
    "strategy_id": "equity.cross-sectional-momentum-topk",
    "revision": 1,
    "package_hash": "2e938c502c2d617e5215f580f18096350d69ff710797d82c371623c4cb72241e",
}
HISTORICAL_PRICE_LIMIT_LEGACY_ADMISSION = {
    "schema": "quant-runtime.price-limit-admission.v1",
    "status": "historical_legacy",
    "reason": "admitted before price-limit behavioral conformance was required",
    "strategy_package": dict(HISTORICAL_PRICE_LIMIT_PACKAGE_REF),
    "migration": (
        "publish a new strategy-package.v2 revision with implementations.conformance.runtime"
    ),
}


class FormalInputError(PreflightRequestError):
    """A package parameter object cannot be admitted to formal execution."""


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
            legacy_admission = _validate_local_request(
                self.client, self.registry, self.policy_registry, value, request
            )
            required_semantics = _required_semantics(snapshot_value)
            as_of = _as_of(snapshot_value)
            versioned_observation = value["schema"] == "quant-research.runtime-preflight-request.v4"
            if versioned_observation:
                frozen_snapshot, observation = self.data_adapter.freeze_reference_with_observation(
                    request,
                    as_of=as_of,
                    required_semantics=required_semantics,
                )
            else:
                frozen_snapshot = self.data_adapter.freeze_reference(
                    request,
                    as_of=as_of,
                    required_semantics=required_semantics,
                )
                observation = None
            result = {
                "schema": (
                    "quant-research.runtime-preflight-result.v2"
                    if versioned_observation
                    else "quant-research.runtime-preflight-result.v1"
                ),
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
                    **(
                        {"legacy_admission": legacy_admission}
                        if legacy_admission is not None
                        else {}
                    ),
                },
            }
            if observation is not None:
                result["observation"] = observation
            return result
        except FormalInputError as exc:
            return _failure("formal_input_invalid", "formal_input_invalid", str(exc))
        except PriceLimitConformanceRequired as exc:
            return _failure("request_invalid", "price_limit_conformance_required", str(exc))
        except PreflightRequestError as exc:
            return _failure("request_invalid", "preflight_request_invalid", str(exc))
        except SignalSeriesUnavailable as exc:
            return _failure("request_invalid", "signal_series_unavailable", str(exc))
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
    versioned_observation = value["schema"] == "quant-research.runtime-preflight-request.v4"
    expected_result_fields = {"schema", "status", "frozen_snapshot", "evidence"} | (
        {"observation"} if versioned_observation else set()
    )
    if (
        set(result) != expected_result_fields
        or result.get("schema")
        != (
            "quant-research.runtime-preflight-result.v2"
            if versioned_observation
            else "quant-research.runtime-preflight-result.v1"
        )
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
    required_evidence = {"strategy_package", "verification", "data_semantics"}
    sandboxed = value["schema"] in {
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
    }
    if sandboxed:
        required_evidence.add("behavioral_conformance")
    allowed_evidence = required_evidence | (
        {"legacy_admission"}
        if not sandboxed and evidence.get("legacy_admission") is not None
        else set()
    )
    if set(evidence) != allowed_evidence:
        raise PreflightRequestError("frozen preflight evidence fields are invalid")
    if (
        evidence.get("strategy_package") != value["strategy_package"]
        or evidence.get("verification") != snapshot.get("verification")
        or evidence.get("data_semantics") != snapshot.get("data_semantics")
        or (sandboxed and evidence.get("behavioral_conformance") != value["behavioral_conformance"])
    ):
        raise PreflightRequestError("frozen preflight evidence does not match the request")
    if "legacy_admission" in evidence and (
        sandboxed
        or evidence["legacy_admission"] != HISTORICAL_PRICE_LIMIT_LEGACY_ADMISSION
        or evidence["strategy_package"] != HISTORICAL_PRICE_LIMIT_PACKAGE_REF
    ):
        raise PreflightRequestError("frozen preflight legacy admission marker is invalid")
    if versioned_observation:
        _validate_data_observation(result.get("observation"), snapshot_value)
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


def _validate_data_observation(value: object, snapshot: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "status",
        "as_of",
        "sample_count",
        "instrument_sample_counts",
        "data_revision",
        "data_version",
        "dataset_version",
        "catalog_hash",
        "calendar_hash",
        "coverage_hash",
        "reason",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("schema") != "quant-runtime.data-change-observation.v1"
        or value.get("status") != "evaluated"
        or value.get("as_of") != snapshot.get("as_of")
    ):
        raise PreflightRequestError("Runtime data observation is invalid")
    source = snapshot["source"]
    verification = snapshot["verification"]
    assert isinstance(source, Mapping)
    assert isinstance(verification, Mapping)
    if (
        value.get("data_revision") != source.get("data_revision")
        or value.get("data_version") != verification.get("data_version")
        or value.get("dataset_version") != verification.get("dataset_version")
        or any(
            value.get(name) != verification.get(name)
            for name in (
                "catalog_hash",
                "calendar_hash",
                "coverage_hash",
            )
        )
    ):
        raise PreflightRequestError("Runtime data observation identity drifted")
    counts = value.get("instrument_sample_counts")
    if not isinstance(counts, list):
        raise PreflightRequestError("Runtime data observation counts are invalid")
    normalized: list[tuple[str, int]] = []
    for item in counts:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"instrument", "sample_count"}
            or not isinstance(item.get("instrument"), str)
            or not item["instrument"]
            or not isinstance(item.get("sample_count"), int)
            or isinstance(item["sample_count"], bool)
            or item["sample_count"] < 0
        ):
            raise PreflightRequestError("Runtime data observation counts are invalid")
        normalized.append((str(item["instrument"]), int(item["sample_count"])))
    if normalized != sorted(set(normalized)):
        raise PreflightRequestError("Runtime data observation counts are not canonical")
    if value.get("sample_count") != sum(count for _, count in normalized):
        raise PreflightRequestError("Runtime data observation total is invalid")


def _requires_price_limit_receipt(package_record: Mapping[str, Any]) -> bool:
    manifest = package_record.get("manifest")
    if not isinstance(manifest, Mapping):
        raise PreflightRequestError("registered package manifest is invalid")
    if manifest.get("schema") != "quant-research.strategy-package.v2":
        return False
    requirements = manifest.get("requirements")
    if not isinstance(requirements, Mapping):
        raise PreflightRequestError("registered package requirements are invalid")
    capabilities = requirements.get("capabilities")
    if not isinstance(capabilities, list):
        raise PreflightRequestError("registered package capabilities are invalid")
    return "market.cn.equity.price_limit" in capabilities


def _legacy_price_limit_admission(package_record: Mapping[str, Any]) -> dict[str, Any] | None:
    manifest = package_record.get("manifest")
    if not isinstance(manifest, Mapping):
        raise PreflightRequestError("registered package manifest is invalid")
    requirements = manifest.get("requirements")
    if not isinstance(requirements, Mapping):
        raise PreflightRequestError("registered package requirements are invalid")
    capabilities = requirements.get("capabilities")
    if not isinstance(capabilities, list):
        raise PreflightRequestError("registered package capabilities are invalid")
    if (
        manifest.get("schema") != "quant-research.strategy-package.v1"
        or "market.cn.equity.price_limit" not in capabilities
    ):
        return None
    package_ref = package_record.get("package_ref")
    if package_ref == HISTORICAL_PRICE_LIMIT_PACKAGE_REF:
        return dict(HISTORICAL_PRICE_LIMIT_LEGACY_ADMISSION)
    raise PriceLimitConformanceRequired(
        "price-limit behavioral conformance receipt is required for new strategy-package.v1 "
        "declarations; schema v1 cannot express implementations.conformance, so publish a "
        "new conformance-enabled strategy-package.v2 revision"
    )


def _validate_local_request(
    client: WorkspacePreflightClientPort,
    registry: AdapterRegistry,
    policy_registry: SandboxPolicyRegistry,
    value: Mapping[str, Any],
    request: SnapshotRequest,
) -> dict[str, Any] | None:
    package_record = client.get_registered_package(value["strategy_package"])
    try:
        value["parameters"] = client.validate_parameters(
            value["strategy_package"], value["parameters"]
        )
    except WorkspaceError as exc:
        raise FormalInputError(exc.message) from exc
    legacy_admission = _legacy_price_limit_admission(package_record)
    # New packages cannot bypass conformance by choosing a legacy draft schema.
    price_limit_receipt = _requires_price_limit_receipt(package_record)
    sandboxed = value["schema"] in {
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
    }
    if price_limit_receipt and not sandboxed:
        raise PriceLimitConformanceRequired(
            "price-limit behavioral conformance receipt is required; "
            "use a sandboxed preflight request with the current receipt"
        )
    resolved = (
        policy_registry.resolve(package_record, value["sandbox_profile"]) if sandboxed else None
    )
    if resolved is not None and not price_limit_receipt:
        _verify_conformance(client, package_record, value, resolved.identity_hash)
    _required_semantics(value["snapshot_request"])
    _as_of(value["snapshot_request"])
    with TemporaryDirectory(prefix="quant-runtime-preflight-") as temporary:
        package = VerifiedPackageMaterializer(client).materialize(
            package_record,
            Path(temporary) / "package",
        )
        if price_limit_receipt:
            assert resolved is not None
            _verify_conformance(
                client,
                package_record,
                value,
                resolved.identity_hash,
                scenario_hash=_price_limit_scenario_hash(package),
            )
        package.require_signal_series(request.adjustment)
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
    return legacy_admission


def _draft(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = dict(value)
    base = {
        "schema",
        "strategy_package",
        "snapshot_request",
        "parameters",
        "execution",
    }
    optional_admission = {"genome_admission"}
    sandboxed = {"sandbox_profile", "behavioral_conformance"}
    if set(draft) not in {
        frozenset(base),
        frozenset(base | sandboxed),
        frozenset(base | optional_admission),
        frozenset(base | sandboxed | optional_admission),
    }:
        raise PreflightRequestError("preflight draft has unsupported or missing fields")
    if draft["schema"] not in {
        "quant-research.runtime-preflight-request.v1",
        "quant-research.runtime-preflight-request.v2",
        "quant-research.runtime-preflight-request.v3",
        "quant-research.runtime-preflight-request.v4",
    }:
        raise PreflightRequestError("preflight draft schema is invalid")
    if (
        draft["schema"]
        in {
            "quant-research.runtime-preflight-request.v1",
            "quant-research.runtime-preflight-request.v4",
        }
        and set(draft) != base
    ):
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
    if "genome_admission" in draft:
        if not isinstance(draft["genome_admission"], Mapping):
            raise PreflightRequestError("genome admission must be an object")
        normalized["genome_admission"] = dict(draft["genome_admission"])
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


def _price_limit_scenario_hash(package: StrategyPackage) -> str:
    """Read the T4A scenario identity from verified, non-executable package bytes."""

    provenance = package.manifest.get("provenance", {})
    binding_path = provenance.get("binding_path")
    if not isinstance(binding_path, str):
        raise PreflightRequestError("price-limit conformance provenance is missing")
    path = (package.root / binding_path).resolve()
    if not path.is_relative_to(package.root.resolve()):
        raise PreflightRequestError("price-limit conformance provenance path is invalid")
    try:
        raw = path.read_bytes()
        binding = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightRequestError("price-limit conformance provenance is malformed") from exc
    if sha256_bytes(raw) != provenance.get("binding_sha256") or not isinstance(binding, Mapping):
        raise PreflightRequestError("price-limit conformance provenance identity is invalid")
    conformance = binding.get("conformance")
    if (
        binding.get("schema") != "strategy-workspace.package-provenance-binding.v1"
        or binding.get("strategy_id") != package.strategy_id
        or binding.get("revision") != package.revision
        or binding.get("records") != provenance.get("records")
        or not isinstance(conformance, Mapping)
        or conformance.get("scenario_set_id") != "strategy-workspace.price-limit.v1"
        or conformance.get("entrypoint") != package.implementations("conformance").get("runtime")
        or not isinstance(conformance.get("entrypoint"), str)
    ):
        raise PreflightRequestError("price-limit conformance provenance binding is invalid")
    digest = conformance.get("scenario_hash")
    if not _is_sha256(digest):
        raise PreflightRequestError("price-limit conformance scenario hash is invalid")
    return str(digest)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verify_conformance(
    client: WorkspacePreflightClientPort,
    package_record: Mapping[str, Any],
    draft: Mapping[str, Any],
    profile_hash: str,
    *,
    scenario_hash: str | None = None,
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
        raise PreflightRequestError("behavioral conformance receipt reports a failed outcome")
    if (
        not isinstance(reference.get("conformance_id"), str)
        or not reference["conformance_id"].startswith("sha256:")
        or not _is_sha256(reference["conformance_id"][len("sha256:") :])
        or not all(
            _is_sha256(reference.get(key))
            for key in ("package_hash", "parameters_hash", "profile_hash", "scenario_hash")
        )
    ):
        raise PreflightRequestError("behavioral conformance receipt is malformed")
    expected = {
        "package_hash": package_record["package_ref"]["package_hash"],
        "parameters_hash": sha256_value(dict(draft["parameters"])),
        "profile_hash": profile_hash,
    }
    if scenario_hash is not None:
        expected["scenario_hash"] = scenario_hash
    for key, item in expected.items():
        if reference.get(key) != item:
            raise PreflightRequestError(
                f"behavioral conformance {key.removesuffix('_hash')} hash does not match the run"
            )
    artifact = reference.get("artifact")
    if not isinstance(artifact, Mapping):
        raise PreflightRequestError("behavioral conformance receipt artifact is malformed")
    artifact_required = {
        "schema",
        "uri",
        "sha256",
        "bytes",
        "media_type",
        "record_schema",
        "logical_role",
        "name",
    }
    if (
        set(artifact) != artifact_required
        or artifact.get("schema") != "quant-research.artifact-ref.v1"
        or not isinstance(artifact.get("uri"), str)
        or not _is_sha256(artifact.get("sha256"))
        or not isinstance(artifact.get("bytes"), int)
        or isinstance(artifact.get("bytes"), bool)
        or artifact["bytes"] < 0
        or artifact.get("record_schema") != "quant-runtime.behavioral-conformance-evidence.v1"
        or artifact.get("logical_role") != "behavioral-conformance"
    ):
        raise PreflightRequestError("behavioral conformance receipt artifact is malformed")
    try:
        verification = client.verify_artifact(str(artifact["uri"]))
    except Exception as exc:
        raise PreflightRequestError(
            "behavioral conformance receipt artifact is missing or stale"
        ) from exc
    verified = verification.get("artifact", {})
    if verification.get("verified") is not True or verified != artifact:
        raise PreflightRequestError("behavioral conformance receipt artifact readback mismatch")
    try:
        publication = client.get_record(str(reference["conformance_id"]))
    except Exception as exc:
        raise PreflightRequestError(
            "behavioral conformance receipt publication is missing or stale"
        ) from exc
    if not isinstance(publication, Mapping):
        raise PreflightRequestError("behavioral conformance receipt publication is malformed")
    if (
        publication.get("record_id") != reference["conformance_id"]
        or publication.get("record_type") != "quant-runtime.behavioral-conformance.v1"
        or publication.get("artifacts") != [artifact]
    ):
        raise PreflightRequestError(
            "behavioral conformance receipt publication does not match its reference"
        )
    evidence = publication.get("payload")
    if not isinstance(evidence, Mapping):
        raise PreflightRequestError("behavioral conformance receipt evidence is malformed")
    if scenario_hash is not None:
        payload = canonical_json(dict(evidence))
        digest = sha256_bytes(payload)
        if (
            reference["conformance_id"] != "sha256:" + digest
            or artifact["sha256"] != digest
            or artifact["bytes"] != len(payload)
        ):
            raise PreflightRequestError(
                "behavioral conformance receipt canonical evidence identity is invalid"
            )
    if (
        evidence.get("schema") != "quant-runtime.behavioral-conformance-evidence.v1"
        or evidence.get("evidence_level") != "behavioral-conformance"
        or evidence.get("outcome")
        not in ({"passed"} if scenario_hash is not None else {None, "passed"})
        or any(evidence.get(key) != reference.get(key) for key in (*expected, "scenario_hash"))
    ):
        raise PreflightRequestError("behavioral conformance receipt evidence identity is invalid")
    dimensions = evidence.get("dimensions")
    trace = evidence.get("trace")
    if (
        not isinstance(dimensions, Mapping)
        or set(dimensions) != DIMENSIONS
        or any(
            not isinstance(item, Mapping) or item.get("status") != "passed"
            for item in dimensions.values()
        )
        or not isinstance(trace, list)
        or (scenario_hash is not None and not trace)
        or (
            scenario_hash is not None
            and any(
                not isinstance(item, Mapping) or item.get("status") != "passed" for item in trace
            )
        )
    ):
        raise PreflightRequestError("behavioral conformance receipt reports a failed outcome")


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
