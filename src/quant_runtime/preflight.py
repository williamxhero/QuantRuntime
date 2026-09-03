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
from quant_runtime.capabilities import AdapterRegistry
from quant_runtime.materialization import VerifiedPackageMaterializer
from quant_runtime.registry import production_registry


class WorkspacePreflightClientPort(Protocol):
    def get_registered_package(self, package_ref: Mapping[str, Any]) -> dict[str, Any]: ...
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Path) -> dict[str, Any]: ...


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
    ) -> None:
        self.client = client
        self.registry = registry or production_registry()
        self.data_adapter = data_adapter or MarketHubDataAdapter()

    def preflight(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = _draft(draft)
            snapshot_value = _snapshot_request(value["snapshot_request"])
            request = SnapshotRequest.from_dict(snapshot_value)
            required_semantics = _required_semantics(snapshot_value)
            as_of = _as_of(snapshot_value)
            with TemporaryDirectory(prefix="quant-runtime-preflight-") as temporary:
                package = VerifiedPackageMaterializer(self.client).materialize(
                    self.client.get_registered_package(value["strategy_package"]),
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
    if set(draft) != {
        "schema",
        "strategy_package",
        "snapshot_request",
        "parameters",
        "execution",
    }:
        raise PreflightRequestError("preflight draft has unsupported or missing fields")
    if draft["schema"] != "quant-research.runtime-preflight-request.v1":
        raise PreflightRequestError("preflight draft schema is invalid")
    if not isinstance(draft["strategy_package"], Mapping):
        raise PreflightRequestError("preflight draft strategy_package must be an object")
    if not isinstance(draft["snapshot_request"], Mapping):
        raise PreflightRequestError("preflight draft snapshot_request must be an object")
    if not isinstance(draft["parameters"], Mapping) or not isinstance(draft["execution"], Mapping):
        raise PreflightRequestError("preflight draft parameters and execution must be objects")
    return {
        **draft,
        "strategy_package": dict(draft["strategy_package"]),
        "snapshot_request": dict(draft["snapshot_request"]),
        "parameters": dict(draft["parameters"]),
        "execution": dict(draft["execution"]),
    }


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
