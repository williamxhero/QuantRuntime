from __future__ import annotations

from copy import deepcopy

import pytest

from quant_runtime.a0_baseline import A0BaselineBuilder, A0BaselineError


def _preflight() -> dict:
    return {
        "schema": "quant-research.runtime-preflight-result.v1",
        "status": "accepted",
        "frozen_snapshot": {
            "snapshot_id": "sha256:" + "a" * 64,
            "source": {
                "adapter": "markethub",
                "base_url": "http://yosef-server:8803",
                "data_revision": "global-v1",
            },
            "query": {"instruments": ["SH.600000"], "frequency": "1d"},
            "calendar": "cn-equity-v1",
            "as_of": "2025-02-01T00:00:00Z",
            "verification": {"coverage_hash": "b" * 64},
            "data_semantics": {
                "field_availability": {"status": "verified", "reason": "public"},
                "point_in_time": {"status": "verified", "reason": "public"},
                "time": {"status": "verified", "reason": "public"},
            },
        },
    }


def test_baseline_is_deterministic_and_keeps_four_role_policy() -> None:
    kwargs = {
        "preflight": _preflight(),
        "capabilities": {
            "schema": "quant-research.runtime-capability.v1",
            "capabilities": ["market.cn.equity"],
        },
        "method_matrix": [{"method": "preflight", "status": "supported"}],
    }
    first = A0BaselineBuilder().build(**kwargs)
    second = A0BaselineBuilder().build(**deepcopy(kwargs))
    assert first.baseline_id == second.baseline_id
    assert [role.role for role in first.roles] == [
        "synthetic_fixture",
        "regression_snapshot",
        "development_validation",
        "holdout",
    ]
    assert first.readiness["status"] == "ready"
    assert first.roles[-1].protected is True
    serialized = first.as_dict()
    assert {
        "a0_data_manifest",
        "a0_data_readiness",
        "a0_execution_capabilities",
        "a0_method_matrix",
    } <= serialized.keys()


def test_unverified_semantics_are_blocked_and_recorded_as_a_gap() -> None:
    value = _preflight()
    value["frozen_snapshot"]["data_semantics"]["point_in_time"]["status"] = "not_evaluated"
    result = A0BaselineBuilder().build(
        preflight=value,
        capabilities={"capabilities": ["market.cn.equity"]},
        method_matrix=[{"method": "preflight", "status": "not_evaluated"}],
    )
    assert result.readiness["status"] == "blocked"
    assert "data_semantics:point_in_time" in result.gaps


def test_fixture_source_cannot_be_reported_as_real_readiness() -> None:
    value = _preflight()
    value["frozen_snapshot"]["source"]["base_url"] = "http://fixture"
    result = A0BaselineBuilder().build(
        preflight=value,
        capabilities={"capabilities": ["market.cn.equity"]},
        method_matrix=[{"method": "preflight", "status": "supported"}],
    )
    assert result.readiness["status"] == "blocked"
    assert "real_market_data_source" in result.gaps


def test_baseline_rejects_missing_public_snapshot() -> None:
    value = _preflight()
    del value["frozen_snapshot"]["verification"]
    with pytest.raises(A0BaselineError, match="source/query/verification"):
        A0BaselineBuilder().build(
            preflight=value,
            capabilities={"capabilities": ["market.cn.equity"]},
            method_matrix=[],
        )
