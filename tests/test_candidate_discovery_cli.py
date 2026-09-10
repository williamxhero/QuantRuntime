from __future__ import annotations

import base64
import io
import json
import platform
import sys
from pathlib import Path

import joblib
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


def _model_artifact(client: WorkspaceClient) -> dict[str, object]:
    rows = ["timestamp,instrument,close,forward_return"]
    for year, close, label in (
        (2020, 10, 0.01),
        (2020, 20, -0.01),
        (2021, 11, 0.02),
        (2021, 19, -0.02),
        (2022, 12, 0.03),
        (2022, 18, -0.03),
        (2023, 13, 0.04),
        (2023, 17, -0.04),
    ):
        instrument = "A" if close < 15 else "B"
        rows.append(f"{year}-06-01,{instrument},{close},{label}")
    content = ("\n".join(rows) + "\n").encode()
    publication = client.publish_record(
        {
            "record_id": "e" * 64,
            "record_type": "frozen-market-frame.v1",
            "payload": {"schema": "frozen-market-frame.v1", "rows": 8},
        },
        artifacts=(
            {
                "source": content,
                "media_type": "text/csv",
                "record_schema": "market-frame.csv.v1",
                "logical_role": "frozen-input",
                "name": "model-market.csv",
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
                    "field_id": "market.close",
                    "capability": "data.bar.1d",
                    "column": "close",
                    "frequency": "1d",
                    "adjustment": "none",
                    "as_of": "decision_time",
                    "lag_bars": 0,
                    "unit": "price",
                    "null_policy": "reject",
                    "required_semantics": [{"dimension": "point_in_time", "required": True}],
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


def _model_request(artifact: dict[str, object]) -> dict[str, object]:
    request = _factor_request(artifact)
    factor_calculation = dict(request["task"])  # type: ignore[arg-type]
    for key in ("kind", "candidate_revision"):
        factor_calculation.pop(key)
    request["task"] = {
        "kind": "model",
        "candidate_revision": {
            "record_id": "f" * 64,
            "record_type": "apex-research.model-candidate.v1",
            "semantic_id": "1" * 64,
        },
        "features": [
            {
                "feature_name": "close_minus_one",
                "factor_revision": {
                    "record_id": "a" * 64,
                    "record_type": "apex-research.factor-candidate.v1",
                    "semantic_id": "b" * 64,
                },
                "calculation": factor_calculation,
            }
        ],
        "label": {
            "registry": "apex-research.label-registry.v1",
            "field": "forward_return",
            "kind": "forward_return",
            "horizon": 1,
            "frequency": "1d",
            "unit": "return",
        },
        "windows": {
            "train": {"start": "2020-01-01", "end": "2021-12-31"},
            "validation": {"start": "2022-01-01", "end": "2022-12-31"},
            "test": {"start": "2023-01-01", "end": "2023-12-31"},
        },
        "fit_timestamp": {
            "policy": "after_training_window",
            "timestamp": "2022-01-01T00:00:00Z",
        },
        "estimator": {
            "registry": "apex-research.estimator-registry.v1",
            "kind": "ridge",
            "hyperparameters": {"alpha": 1.0, "fit_intercept": True},
        },
        "seeds": [{"purpose": "estimator", "value": 7}],
        "training_environment": {
            "runtime": "cpython",
            "runtime_version": platform.python_version(),
            "platform": _runtime_platform(),
            "dependency_lock_sha256": CANDIDATE_DISCOVERY_LOCK_SHA256,
            "container_image_sha256": None,
        },
        "source_artifact": artifact,
    }
    return request


def _runtime_platform() -> str:
    machine = platform.machine().lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    operating_system = "windows" if sys.platform == "win32" else "linux"
    return f"{operating_system}-{architecture}"


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


def test_candidate_discovery_trains_model_and_replays_exact_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_path = tmp_path / "workspace"
    client = WorkspaceClient(workspace_path)
    for record_id, record_type in (
        ("a" * 64, "apex-research.factor-candidate.v1"),
        ("f" * 64, "apex-research.model-candidate.v1"),
    ):
        client.publish_record(
            {
                "record_id": record_id,
                "record_type": record_type,
                "payload": {"schema": "opaque-owner-fact.v1"},
            }
        )
    request = _model_request(_model_artifact(client))
    request_path = tmp_path / "model-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    argv = [
        "candidate-discovery",
        "--workspace",
        str(workspace_path),
        "--request",
        str(request_path),
    ]

    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["task_kind"] == "model"
    assert first["evidence_level"] == "discovery-only"
    trained = next(item for item in first["artifacts"] if item["name"] == "model.joblib")
    assert trained["logical_role"] == "trained-model"
    assert trained["record_schema"] == "quant-runtime.ridge-model.v1"
    assert client.verify_artifact(trained["uri"])["verified"] is True
    readback = client.read_artifact(trained["uri"])
    model_payload = joblib.load(io.BytesIO(base64.b64decode(readback["content"], validate=True)))
    assert "request_id" not in model_payload
    assert model_payload["data"]["artifact"]["sha256"] == request["data"]["artifact"]["sha256"]
    assert model_payload["runtime_environment"]["platform"] == _runtime_platform()
