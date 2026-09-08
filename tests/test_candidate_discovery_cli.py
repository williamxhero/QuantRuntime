from __future__ import annotations

import json
from pathlib import Path

import pytest
from strategy_workspace import WorkspaceClient

from quant_runtime.candidate_discovery import CANDIDATE_DISCOVERY_LOCK_SHA256
from quant_runtime.cli import main, runtime_capabilities


def _artifact(client: WorkspaceClient) -> dict[str, object]:
    content = (
        b"timestamp,instrument,close,forward_return\n"
        b"2025-01-01,A,10,0.01\n"
        b"2025-01-01,B,20,-0.01\n"
        b"2025-01-02,A,12,0.02\n"
        b"2025-01-02,B,18,-0.02\n"
    )
    publication = client.publish_record(
        {
            "record_id": "d" * 64,
            "record_type": "frozen-market-frame.v1",
            "payload": {"schema": "frozen-market-frame.v1", "rows": 4},
        },
        artifacts=(
            {
                "source": content,
                "media_type": "text/csv",
                "record_schema": "market-frame.csv.v1",
                "logical_role": "frozen-input",
                "name": "market.csv",
            },
        ),
    )
    return dict(publication["artifacts"][0])


def _factor_request(artifact: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "quant-runtime.candidate-discovery-request.v1",
        "task": {
            "kind": "factor",
            "candidate_revision": {
                "record_id": "a" * 64,
                "record_type": "apex-research.factor-candidate.v1",
                "semantic_id": "b" * 64,
            },
            "expression": {
                "kind": "subtract",
                "left": {"kind": "field", "name": "close"},
                "right": {"kind": "constant", "value": 1},
            },
            "inputs": [
                {
                    "name": "close",
                    "field": "close",
                    "frequency": "1d",
                    "adjustment": "none",
                    "as_of": "decision_time",
                    "lag_bars": 0,
                    "unit": "price",
                    "null_policy": "reject",
                }
            ],
            "output": {"dtype": "float64", "unit": "price"},
            "warm_up": {"periods": 0, "behavior": "emit_null"},
            "missing_values": "propagate",
        },
        "data": {
            "artifact": artifact,
            "format": "market-frame.csv.v1",
            "timestamp_column": "timestamp",
            "instrument_column": "instrument",
            "label_column": "forward_return",
            "calendar": "XSHG",
            "frequency": "1d",
            "adjustment": "none",
            "as_of": "2025-01-02T16:00:00Z",
            "point_in_time": True,
        },
        "environment": {
            "backend_id": "qlib",
            "adapter_version": "candidate-discovery.v1",
            "dependency_lock_sha256": CANDIDATE_DISCOVERY_LOCK_SHA256,
        },
        "limits": {"max_rows": 100, "max_output_bytes": 100000},
    }


def test_candidate_discovery_cli_calculates_factor_from_frozen_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_path = tmp_path / "workspace"
    client = WorkspaceClient(workspace_path)
    client.publish_record(
        {
            "record_id": "a" * 64,
            "record_type": "apex-research.factor-candidate.v1",
            "payload": {"schema": "opaque-owner-fact.v1"},
        }
    )
    request = _factor_request(_artifact(client))
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert (
        main(
            [
                "candidate-discovery",
                "--workspace",
                str(workspace_path),
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "completed"
    assert output["evidence_level"] == "discovery-only"
    assert output["backend_id"] == "qlib"
    assert output["task_kind"] == "factor"
    assert output["metrics"]["non_null_count"] == 4
    assert output["result"]["record_type"] == "quant-runtime.candidate-discovery.v1"
    publication = client.get_record(output["result"]["record_id"])
    assert publication["payload"]["request_id"] == output["request_id"]
    assert len(publication["artifacts"]) == 2

    capabilities = runtime_capabilities()
    assert "candidate-discovery.v1" in capabilities["capabilities"]


def test_candidate_discovery_rejects_executable_factor_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_path = tmp_path / "workspace"
    client = WorkspaceClient(workspace_path)
    request = _factor_request(_artifact(client))
    request["task"] = {
        **request["task"],  # type: ignore[dict-item]
        "entrypoint": "malicious.module:run",
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert (
        main(
            [
                "candidate-discovery",
                "--workspace",
                str(workspace_path),
                "--request",
                str(request_path),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    with pytest.raises(Exception, match="record not found"):
        client.get_record("a" * 64)
