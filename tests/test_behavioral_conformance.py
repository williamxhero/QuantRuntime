from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from conftest import PACKAGE
from strategy_workspace import WorkspaceClient
from test_preflight import draft as preflight_draft
from test_preflight import preflight
from test_sandbox_policy import isolated_profile

from quant_runtime.conformance import RuntimeConformance

DIMENSIONS = {
    name: {"status": "passed", "observed": "fixture"}
    for name in (
        "decision_time",
        "warm_up",
        "strict_comparison",
        "entry",
        "exit",
        "sizing",
        "state_transition",
        "add_reduce",
    )
}


class ConformanceBackend:
    production = False

    def __init__(self, *, status: str = "passed") -> None:
        self.status = status
        self.calls = 0

    def invoke(self, prepared):
        self.calls += 1
        return {
            "schema": "quant-runtime.sandbox-worker-result.v1",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "success",
            "payload": {
                "schema": "quant-runtime.behavioral-conformance.v1",
                "status": self.status,
                "dimensions": DIMENSIONS,
                "trace": [{"scenario": "strict-boundary", "event": 1, "decision": "hold"}],
            },
            "diagnostics": {
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "artifacts": 0,
                "truncated": False,
                "sanitized": True,
            },
        }


def _request(client: WorkspaceClient, package: dict, *, scenario: bytes = b"{}") -> dict:
    publication = client.publish_record(
        {
            "record_id": "scenario-" + str(len(client.list_records())),
            "record_type": "quant-runtime.behavioral-scenarios.v1",
            "payload": {},
        },
        artifacts=(
            {
                "source": scenario,
                "media_type": "application/json",
                "record_schema": "quant-runtime.behavioral-scenarios.v1",
                "logical_role": "behavioral-scenario",
                "name": "scenarios.json",
            },
        ),
    )
    profile = isolated_profile()
    profile["trust_classification"] = "human_isolated"
    return {
        "schema": "quant-research.runtime-conformance-request.v1",
        "strategy_package": package["package_ref"],
        "parameters": {},
        "sandbox_profile": profile,
        "behavioral_scenarios": publication["artifacts"],
    }


def test_behavioral_pass_publishes_one_identity_bound_non_formal_evidence(
    tmp_path: Path, market_fixture: dict
) -> None:
    client = WorkspaceClient(tmp_path / "workspace")
    package = client.register_package(PACKAGE)
    backend = ConformanceBackend()
    request = _request(client, package)

    result = RuntimeConformance(client, backend=backend).conform(request)

    assert result["status"] == "accepted"
    reference = result["behavioral_conformance"]
    assert reference["status"] == "passed"
    assert reference["evidence_level"] == "behavioral-conformance"
    publication = client.get_record(reference["conformance_id"])
    assert publication["record_type"] == "quant-runtime.behavioral-conformance.v1"
    assert publication["artifacts"] == [reference["artifact"]]
    assert client.verify_artifact(reference["artifact"]["uri"])["verified"] is True
    assert client.list_runs() == []
    assert "formal" not in publication["payload"]
    assert "performance" not in publication["payload"]
    assert "qualification" not in publication["payload"]
    assert backend.calls == 1

    replay = RuntimeConformance(client, backend=backend).conform(request)
    assert replay["behavioral_conformance"] == reference

    run_draft = preflight_draft(package["package_ref"])
    run_draft.update(
        {
            "schema": "quant-research.runtime-preflight-request.v2",
            "sandbox_profile": request["sandbox_profile"],
            "behavioral_conformance": reference,
        }
    )
    prepared = preflight(tmp_path / "workspace", market_fixture).preflight(run_draft)
    assert prepared["status"] == "accepted"
    submitted = client.submit_run(
        {
            "schema": "quant-research.workspace-run-request.v4",
            "strategy_package": package["package_ref"],
            "market_snapshot": prepared["frozen_snapshot"],
            "parameters": {},
            "sandbox_profile": request["sandbox_profile"],
            "behavioral_conformance": reference,
            "execution": run_draft["execution"],
        }
    )
    assert submitted["request"]["behavioral_conformance"] == reference


def test_behavioral_rejection_publishes_no_pass_and_is_scenario_sensitive(
    tmp_path: Path,
) -> None:
    client = WorkspaceClient(tmp_path / "workspace")
    package = client.register_package(PACKAGE)
    request = _request(client, package)
    before = list(client.list_records())

    rejected = RuntimeConformance(client, backend=ConformanceBackend(status="rejected")).conform(
        request
    )

    assert rejected["status"] == "rejected"
    assert rejected["observation"]["classification"] == "strategy_rejection"
    assert client.list_records() == before
    changed = deepcopy(request)
    changed["behavioral_scenarios"] = _request(client, package, scenario=b'{"changed":true}')[
        "behavioral_scenarios"
    ]
    passed = RuntimeConformance(client, backend=ConformanceBackend()).conform(changed)
    assert passed["behavioral_conformance"]["scenario_hash"] != "0" * 64
