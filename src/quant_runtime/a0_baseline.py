"""A0 data-role and readiness evidence assembled from public Runtime facts.

This module deliberately does not fetch data or inspect Workspace internals.  It
turns an already obtained MarketHub preflight result and Runtime capability
receipt into a deterministic, auditable baseline for A0-T03.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["synthetic_fixture", "regression_snapshot", "development_validation", "holdout"]
Status = Literal["ready", "blocked", "not_evaluated", "failed"]

_ROLES: tuple[Role, ...] = (
    "synthetic_fixture",
    "regression_snapshot",
    "development_validation",
    "holdout",
)


class A0BaselineError(ValueError):
    """Raised when public evidence cannot form a safe A0 baseline."""


@dataclass(frozen=True, slots=True)
class DataRolePolicy:
    role: Role
    daily_use: str
    allowed_in_ci: bool
    allowed_for_selection: bool
    protected: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "daily_use": self.daily_use,
            "allowed_in_ci": self.allowed_in_ci,
            "allowed_for_selection": self.allowed_for_selection,
            "protected": self.protected,
        }


@dataclass(frozen=True, slots=True)
class A0DataBaseline:
    schema: str
    revision: str
    source: Mapping[str, object]
    snapshot: Mapping[str, object]
    roles: tuple[DataRolePolicy, ...]
    readiness: Mapping[str, object]
    execution_capabilities: Mapping[str, object]
    method_matrix: tuple[Mapping[str, object], ...]
    gaps: tuple[str, ...]
    baseline_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "a0_data_manifest": {
                "source": dict(self.source),
                "snapshot": dict(self.snapshot),
            },
            "roles": [role.as_dict() for role in self.roles],
            "a0_data_readiness": dict(self.readiness),
            "a0_execution_capabilities": dict(self.execution_capabilities),
            "a0_method_matrix": [dict(item) for item in self.method_matrix],
            "gaps": list(self.gaps),
            "baseline_id": self.baseline_id,
        }


class A0BaselineBuilder:
    """Build a baseline without converting fixtures into live readiness."""

    revision = "a0-t03.v1"

    def build(
        self,
        *,
        preflight: Mapping[str, Any],
        capabilities: Mapping[str, Any],
        method_matrix: Sequence[Mapping[str, Any]],
    ) -> A0DataBaseline:
        if not isinstance(preflight, Mapping) or not isinstance(capabilities, Mapping):
            raise A0BaselineError("baseline inputs must be mappings")
        if preflight.get("schema") not in {
            "quant-research.runtime-preflight-result.v1",
            "quant-research.runtime-preflight-result.v2",
        }:
            raise A0BaselineError("unsupported preflight evidence schema")
        snapshot = preflight.get("frozen_snapshot")
        if not isinstance(snapshot, Mapping):
            raise A0BaselineError("baseline requires a frozen MarketHub snapshot")
        source = snapshot.get("source")
        query = snapshot.get("query")
        verification = snapshot.get("verification")
        if not all(isinstance(value, Mapping) for value in (source, query, verification)):
            raise A0BaselineError("snapshot lacks public source/query/verification evidence")
        readiness = _readiness(snapshot, preflight)
        gaps = _gaps(snapshot, capabilities, method_matrix)
        baseline_payload = {
            "schema": "quant-runtime.a0-data-baseline.v1",
            "revision": self.revision,
            "source": dict(source),
            "snapshot": {
                "snapshot_id": snapshot.get("snapshot_id"),
                "query": dict(query),
                "calendar": snapshot.get("calendar"),
                "as_of": snapshot.get("as_of"),
                "verification": dict(verification),
            },
            "readiness": readiness,
            "execution_capabilities": dict(capabilities),
            "method_matrix": [dict(item) for item in method_matrix],
            "gaps": gaps,
        }
        digest = hashlib.sha256(_canonical(baseline_payload)).hexdigest()
        return A0DataBaseline(
            schema="quant-runtime.a0-data-baseline.v1",
            revision=self.revision,
            source=dict(source),
            snapshot=baseline_payload["snapshot"],
            roles=_default_roles(),
            readiness=readiness,
            execution_capabilities=dict(capabilities),
            method_matrix=tuple(dict(item) for item in method_matrix),
            gaps=gaps,
            baseline_id=f"sha256:{digest}",
        )


def _default_roles() -> tuple[DataRolePolicy, ...]:
    return (
        DataRolePolicy("synthetic_fixture", "结构与负向工程测试", True, False, False),
        DataRolePolicy("regression_snapshot", "真实工程回归", True, False, False),
        DataRolePolicy("development_validation", "研究开发与验证样本", False, True, False),
        DataRolePolicy("holdout", "最终保留样本", False, False, True),
    )


def _readiness(snapshot: Mapping[str, Any], preflight: Mapping[str, Any]) -> dict[str, object]:
    source = snapshot.get("source")
    base_url = source.get("base_url") if isinstance(source, Mapping) else None
    real_source = (
        isinstance(base_url, str) and base_url.startswith("http") and "fixture" not in base_url
    )
    semantics = snapshot.get("data_semantics")
    verification = snapshot.get("verification")
    statuses = []
    if isinstance(semantics, Mapping):
        statuses = [item.get("status") for item in semantics.values() if isinstance(item, Mapping)]
    complete = (
        real_source and bool(verification) and all(status == "verified" for status in statuses)
    )
    return {
        "status": "ready" if preflight.get("status") == "accepted" and complete else "blocked",
        "preflight_status": preflight.get("status"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "data_semantics": dict(semantics) if isinstance(semantics, Mapping) else {},
        "reason": (
            "all public semantics verified from a non-fixture source"
            if complete
            else "fixture or non-real source cannot establish live data readiness"
            if not real_source
            else "one or more semantics are not verified"
        ),
    }


def _gaps(
    snapshot: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    method_matrix: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    gaps: list[str] = []
    source = snapshot.get("source")
    base_url = source.get("base_url") if isinstance(source, Mapping) else None
    if not isinstance(base_url, str) or not base_url.startswith("http") or "fixture" in base_url:
        gaps.append("real_market_data_source")
    semantics = snapshot.get("data_semantics")
    if isinstance(semantics, Mapping):
        for name, item in semantics.items():
            if not isinstance(item, Mapping) or item.get("status") != "verified":
                gaps.append(f"data_semantics:{name}")
    if not capabilities.get("capabilities"):
        gaps.append("runtime_capabilities")
    for item in method_matrix:
        if item.get("status") not in {"supported", "not_evaluated", "blocked"}:
            gaps.append(f"method:{item.get('method', 'unknown')}")
    return tuple(sorted(set(gaps)))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


__all__ = ["A0BaselineBuilder", "A0BaselineError", "A0DataBaseline", "DataRolePolicy"]
