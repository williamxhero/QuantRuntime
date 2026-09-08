"""Closed declarative Factor and Model discovery owned by Quant Runtime."""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pandas as pd
import qlib
from strategy_workspace import WorkspaceClient

from quant_runtime.artifacts import canonical_json, sha256_bytes, sha256_value

CANDIDATE_DISCOVERY_LOCK_SHA256 = "dd4bf303e363a6bb7e4e9d113609c86e429329470a182da1a877f913427246eb"
REQUEST_SCHEMA = "quant-runtime.candidate-discovery-request.v1"
RESULT_SCHEMA = "quant-runtime.candidate-discovery-result.v1"
RECORD_TYPE = "quant-runtime.candidate-discovery.v1"
MAX_EXPRESSION_DEPTH = 16


class CandidateDiscoveryError(ValueError):
    """The strict discovery request or an owner fact violated its contract."""


class CandidateDiscoveryService:
    def __init__(self, workspace: WorkspaceClient) -> None:
        self._workspace = workspace

    def execute(self, value: Mapping[str, Any]) -> dict[str, Any]:
        request = _request(value)
        request_id = sha256_value(request)
        task = _object(request, "task")
        if task["kind"] != "factor":
            raise CandidateDiscoveryError("candidate discovery task kind is unsupported")
        candidate = _object(task, "candidate_revision")
        self._require_candidate(candidate)
        frame = self._read_frame(_object(request, "data"), _object(request, "limits"))
        values = _factor_values(frame, task)
        output = frame[
            [request["data"]["timestamp_column"], request["data"]["instrument_column"]]
        ].copy()
        output["value"] = values
        csv_bytes = output.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode(
            "utf-8"
        )
        if len(csv_bytes) > request["limits"]["max_output_bytes"]:
            raise CandidateDiscoveryError("candidate discovery output exceeds the byte limit")
        finite = values.dropna()
        metrics: dict[str, int | float | None] = {
            "row_count": int(len(values)),
            "non_null_count": int(len(finite)),
            "mean": float(finite.mean()) if len(finite) else None,
        }
        label = request["data"]["label_column"]
        metrics["mean_rank_ic"] = (
            _mean_rank_ic(
                frame,
                values,
                str(request["data"]["timestamp_column"]),
                str(label),
            )
            if label
            else None
        )
        for metric in metrics.values():
            if isinstance(metric, float) and not math.isfinite(metric):
                raise CandidateDiscoveryError("candidate discovery produced a non-finite metric")
        observed_environment = {
            "backend_id": "qlib",
            "adapter_version": "candidate-discovery.v1",
            "engine_version": qlib.__version__,
            "dependency_lock_sha256": CANDIDATE_DISCOVERY_LOCK_SHA256,
        }
        manifest = {
            "schema": "quant-runtime.candidate-discovery-artifact-manifest.v1",
            "request_id": request_id,
            "task_kind": "factor",
            "candidate_revision": candidate,
            "input_artifact_sha256": request["data"]["artifact"]["sha256"],
            "output_sha256": sha256_bytes(csv_bytes),
            "metrics": metrics,
            "environment": observed_environment,
        }
        manifest_bytes = canonical_json(manifest) + b"\n"
        payload = {
            "schema": RECORD_TYPE,
            "status": "completed",
            "evidence_level": "discovery-only",
            "request_id": request_id,
            "task_kind": "factor",
            "candidate_revision": candidate,
            "data": request["data"],
            "environment": observed_environment,
            "metrics": metrics,
            "artifact_digests": [sha256_bytes(csv_bytes), sha256_bytes(manifest_bytes)],
        }
        record_id = sha256_value(payload)
        publication = {
            "record_id": record_id,
            "record_type": RECORD_TYPE,
            "payload": payload,
            "lineage": [
                {
                    "source_kind": candidate["record_type"],
                    "source_id": candidate["record_id"],
                    "relation": "evaluates-candidate",
                }
            ],
        }
        artifacts = (
            {
                "source": csv_bytes,
                "media_type": "text/csv",
                "record_schema": "quant-runtime.factor-values.v1",
                "logical_role": "candidate-discovery-output",
                "name": "factor-values.csv",
            },
            {
                "source": manifest_bytes,
                "media_type": "application/json",
                "record_schema": manifest["schema"],
                "logical_role": "candidate-discovery-manifest",
                "name": "discovery-manifest.json",
            },
        )
        try:
            current = self._workspace.get_record(record_id)
        except Exception as exc:
            if getattr(exc, "code", None) != "record_not_found" and not isinstance(exc, KeyError):
                raise
            current = self._workspace.publish_record(publication, artifacts=artifacts)
        if (
            current.get("record_id") != record_id
            or current.get("record_type") != RECORD_TYPE
            or current.get("payload") != payload
            or current.get("lineage") != publication["lineage"]
        ):
            raise CandidateDiscoveryError("candidate discovery publication identity conflict")
        published_artifacts = current.get("artifacts")
        if not isinstance(published_artifacts, list) or len(published_artifacts) != 2:
            raise CandidateDiscoveryError("candidate discovery artifacts are incomplete")
        expected = sorted(payload["artifact_digests"])
        observed = sorted(str(item.get("sha256", "")) for item in published_artifacts)
        if observed != expected:
            raise CandidateDiscoveryError("candidate discovery artifact identity mismatch")
        for artifact in published_artifacts:
            verification = self._workspace.verify_artifact(str(artifact["uri"]))
            if verification.get("sha256") != artifact["sha256"]:
                raise CandidateDiscoveryError("candidate discovery artifact verification failed")
        if self._workspace.get_record(record_id) != current:
            raise CandidateDiscoveryError("candidate discovery canonical readback mismatch")
        return {
            "schema": RESULT_SCHEMA,
            "status": "completed",
            "evidence_level": "discovery-only",
            "request_id": request_id,
            "task_kind": "factor",
            "backend_id": "qlib",
            "adapter_version": "candidate-discovery.v1",
            "engine_version": qlib.__version__,
            "metrics": metrics,
            "result": {"record_id": record_id, "record_type": RECORD_TYPE},
            "artifacts": published_artifacts,
        }

    def _require_candidate(self, candidate: Mapping[str, Any]) -> None:
        current = self._workspace.get_record(str(candidate["record_id"]))
        if (
            current.get("record_id") != candidate["record_id"]
            or current.get("record_type") != candidate["record_type"]
        ):
            raise CandidateDiscoveryError("candidate owner reference mismatch")

    def _read_frame(self, data: Mapping[str, Any], limits: Mapping[str, Any]) -> pd.DataFrame:
        artifact = _object(data, "artifact")
        verification = self._workspace.verify_artifact(str(artifact["uri"]))
        if verification.get("sha256") != artifact["sha256"]:
            raise CandidateDiscoveryError("frozen data artifact verification failed")
        readback = self._workspace.read_artifact(str(artifact["uri"]))
        if (
            not isinstance(readback.get("artifact"), Mapping)
            or readback["artifact"].get("sha256") != artifact["sha256"]
        ):
            raise CandidateDiscoveryError("frozen data artifact readback mismatch")
        try:
            content = base64.b64decode(str(readback["content"]), validate=True)
            frame = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            raise CandidateDiscoveryError("frozen market frame is invalid") from exc
        if len(frame) > limits["max_rows"]:
            raise CandidateDiscoveryError("frozen market frame exceeds the row limit")
        required = {
            str(data["timestamp_column"]),
            str(data["instrument_column"]),
            *([str(data["label_column"])] if isinstance(data["label_column"], str) else []),
        }
        if not required.issubset(frame.columns):
            raise CandidateDiscoveryError("frozen market frame lacks required columns")
        return frame


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = dict(value)
    _keys(request, {"schema", "task", "data", "environment", "limits"}, "request")
    if request["schema"] != REQUEST_SCHEMA:
        raise CandidateDiscoveryError("candidate discovery request schema is invalid")
    task = _object(request, "task")
    _keys(
        task,
        {
            "kind",
            "candidate_revision",
            "expression",
            "inputs",
            "output",
            "warm_up",
            "missing_values",
        },
        "Factor task",
    )
    if task["kind"] != "factor":
        raise CandidateDiscoveryError("candidate discovery task kind is invalid")
    candidate = _object(task, "candidate_revision")
    _keys(candidate, {"record_id", "record_type", "semantic_id"}, "candidate")
    _sha(candidate["record_id"], "candidate record")
    _sha(candidate["semantic_id"], "candidate semantic")
    if candidate["record_type"] != "apex-research.factor-candidate.v1":
        raise CandidateDiscoveryError("Factor task candidate type is invalid")
    inputs = task["inputs"]
    if not isinstance(inputs, list) or not inputs or len(inputs) > 64:
        raise CandidateDiscoveryError("Factor inputs are invalid")
    names: set[str] = set()
    for item in inputs:
        if not isinstance(item, Mapping):
            raise CandidateDiscoveryError("Factor input must be an object")
        current = dict(item)
        _keys(
            current,
            {
                "name",
                "field",
                "frequency",
                "adjustment",
                "as_of",
                "lag_bars",
                "unit",
                "null_policy",
            },
            "Factor input",
        )
        name = _bounded(current["name"], "Factor input name")
        field = _bounded(current["field"], "Factor input field")
        if name in names or field.startswith("__"):
            raise CandidateDiscoveryError("Factor input names must be unique and safe")
        names.add(name)
        if current["frequency"] not in {"1d", "1m"}:
            raise CandidateDiscoveryError("Factor input frequency is unsupported")
        if current["adjustment"] not in {"none", "forward", "backward"}:
            raise CandidateDiscoveryError("Factor adjustment is unsupported")
        if current["as_of"] not in {"decision_time", "prior_close"}:
            raise CandidateDiscoveryError("Factor as-of policy is unsupported")
        if not isinstance(current["lag_bars"], int) or not 0 <= current["lag_bars"] <= 10_000:
            raise CandidateDiscoveryError("Factor lag is invalid")
        if current["null_policy"] not in {"reject", "allow"}:
            raise CandidateDiscoveryError("Factor null policy is unsupported")
    _validate_expression(task["expression"], names, 0)
    output = _object(task, "output")
    _keys(output, {"dtype", "unit"}, "Factor output")
    if output["dtype"] != "float64" or not _bounded(output["unit"], "Factor output unit"):
        raise CandidateDiscoveryError("Factor output contract is unsupported")
    warm_up = _object(task, "warm_up")
    _keys(warm_up, {"periods", "behavior"}, "Factor warm-up")
    if not isinstance(warm_up["periods"], int) or not 0 <= warm_up["periods"] <= 100_000:
        raise CandidateDiscoveryError("Factor warm-up is invalid")
    if warm_up["behavior"] not in {"emit_null", "reject"}:
        raise CandidateDiscoveryError("Factor warm-up behavior is unsupported")
    if task["missing_values"] not in {"propagate", "reject"}:
        raise CandidateDiscoveryError("Factor missing-value policy is unsupported")
    data = _object(request, "data")
    _keys(
        data,
        {
            "artifact",
            "format",
            "timestamp_column",
            "instrument_column",
            "label_column",
            "calendar",
            "frequency",
            "adjustment",
            "as_of",
            "point_in_time",
        },
        "data",
    )
    if data["format"] != "market-frame.csv.v1" or data["point_in_time"] is not True:
        raise CandidateDiscoveryError("candidate discovery requires PIT-valid frozen CSV data")
    for name in ("timestamp_column", "instrument_column", "calendar", "as_of"):
        _bounded(data[name], f"data {name}")
    if data["label_column"] is not None:
        _bounded(data["label_column"], "data label column")
    if data["frequency"] not in {"1d", "1m"} or data["adjustment"] not in {
        "none",
        "forward",
        "backward",
    }:
        raise CandidateDiscoveryError("data semantics are unsupported")
    artifact = _object(data, "artifact")
    _keys(
        artifact,
        {
            "schema",
            "uri",
            "sha256",
            "bytes",
            "media_type",
            "record_schema",
            "logical_role",
            "name",
        },
        "data artifact",
    )
    if (
        artifact["schema"] != "quant-research.artifact-ref.v1"
        or artifact["media_type"] != "text/csv"
        or artifact["record_schema"] != "market-frame.csv.v1"
        or not isinstance(artifact["bytes"], int)
        or artifact["bytes"] < 1
    ):
        raise CandidateDiscoveryError("frozen data artifact type is unsupported")
    _sha(artifact["sha256"], "data artifact")
    for name in ("uri", "logical_role", "name"):
        _bounded(artifact[name], f"data artifact {name}")
    environment = _object(request, "environment")
    _keys(
        environment,
        {"backend_id", "adapter_version", "dependency_lock_sha256"},
        "environment",
    )
    if (
        environment["backend_id"] != "qlib"
        or environment["adapter_version"] != "candidate-discovery.v1"
        or environment["dependency_lock_sha256"] != CANDIDATE_DISCOVERY_LOCK_SHA256
    ):
        raise CandidateDiscoveryError("candidate discovery environment drifted")
    limits = _object(request, "limits")
    _keys(limits, {"max_rows", "max_output_bytes"}, "limits")
    for name, maximum in (("max_rows", 10_000_000), ("max_output_bytes", 100_000_000)):
        if not isinstance(limits[name], int) or not 1 <= limits[name] <= maximum:
            raise CandidateDiscoveryError("candidate discovery limits are invalid")
    return request


def _factor_values(frame: pd.DataFrame, task: Mapping[str, Any]) -> pd.Series:
    sources: dict[str, pd.Series] = {}
    for item in task["inputs"]:
        field = str(item["field"])
        if field not in frame.columns:
            raise CandidateDiscoveryError(f"frozen market frame lacks Factor field {field!r}")
        values = pd.to_numeric(frame[field], errors="coerce")
        lag = int(item["lag_bars"])
        sources[str(item["name"])] = values.shift(lag) if lag else values
        if item["null_policy"] == "reject" and sources[str(item["name"])].isna().any():
            raise CandidateDiscoveryError("Factor input contains forbidden null values")
    result = _evaluate_expression(task["expression"], sources)
    warm_up = int(task["warm_up"]["periods"])
    if warm_up:
        result.iloc[:warm_up] = float("nan")
    if task["missing_values"] == "reject" and result.isna().any():
        raise CandidateDiscoveryError("Factor output contains forbidden missing values")
    finite = result.dropna().map(math.isfinite)
    if not bool(finite.all()):
        raise CandidateDiscoveryError("Factor output contains non-finite values")
    return result.astype(float)


def _validate_expression(value: object, names: set[str], depth: int) -> None:
    if depth > MAX_EXPRESSION_DEPTH or not isinstance(value, Mapping):
        raise CandidateDiscoveryError("Factor expression is invalid or too deep")
    node = dict(value)
    kind = node.get("kind")
    if kind == "field":
        _keys(node, {"kind", "name"}, "field expression")
        if node["name"] not in names:
            raise CandidateDiscoveryError("Factor expression references an unknown input")
    elif kind == "constant":
        _keys(node, {"kind", "value"}, "constant expression")
        _finite_number(node["value"], "Factor constant")
    elif kind in {"add", "subtract", "multiply", "divide"}:
        _keys(node, {"kind", "left", "right"}, "binary expression")
        _validate_expression(node["left"], names, depth + 1)
        _validate_expression(node["right"], names, depth + 1)
    elif kind == "negate":
        _keys(node, {"kind", "operand"}, "unary expression")
        _validate_expression(node["operand"], names, depth + 1)
    else:
        raise CandidateDiscoveryError("Factor expression operator is unsupported")


def _evaluate_expression(value: Mapping[str, Any], sources: Mapping[str, pd.Series]) -> pd.Series:
    kind = value["kind"]
    if kind == "field":
        return sources[str(value["name"])].copy()
    if kind == "constant":
        template = next(iter(sources.values()))
        return pd.Series(float(value["value"]), index=template.index)
    if kind == "negate":
        return -_evaluate_expression(value["operand"], sources)
    left = _evaluate_expression(value["left"], sources)
    right = _evaluate_expression(value["right"], sources)
    if kind == "add":
        return left + right
    if kind == "subtract":
        return left - right
    if kind == "multiply":
        return left * right
    denominator = right.replace(0, float("nan"))
    return left / denominator


def _mean_rank_ic(
    frame: pd.DataFrame, values: pd.Series, timestamp_column: str, label_column: str
) -> float | None:
    if label_column not in frame.columns:
        raise CandidateDiscoveryError("frozen market frame lacks the declared label")
    sample = pd.DataFrame(
        {
            "timestamp": frame[timestamp_column],
            "factor": values,
            "label": pd.to_numeric(frame[label_column], errors="coerce"),
        }
    )
    correlations = sample.groupby("timestamp", sort=True).apply(
        lambda group: group["factor"].corr(group["label"], method="spearman"),
        include_groups=False,
    )
    finite = correlations.dropna()
    return float(finite.mean()) if len(finite) else None


def _object(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    current = value.get(key)
    if not isinstance(current, Mapping):
        raise CandidateDiscoveryError(f"candidate discovery {key} must be an object")
    return dict(current)


def _keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CandidateDiscoveryError(f"candidate discovery {label} fields are invalid")


def _sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CandidateDiscoveryError(f"{label} identity is invalid")
    return value


def _bounded(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CandidateDiscoveryError(f"{label} is invalid")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise CandidateDiscoveryError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CandidateDiscoveryError(f"{label} must be finite")
    return result


__all__ = [
    "CANDIDATE_DISCOVERY_LOCK_SHA256",
    "CandidateDiscoveryError",
    "CandidateDiscoveryService",
]
