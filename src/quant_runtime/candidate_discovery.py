"""Closed declarative Factor and Model discovery owned by Quant Runtime."""

from __future__ import annotations

import base64
import io
import math
import platform
import sys
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import joblib
import pandas as pd
import qlib
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
from strategy_workspace import WorkspaceClient

from quant_runtime.artifacts import canonical_json, sha256_bytes, sha256_value

CANDIDATE_DISCOVERY_LOCK_SHA256 = "7708d1ecc05e2a4bc6d4835b31b8eb518e81117f9bda5124dbc807ed1c49f77d"
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
        candidate = _object(task, "candidate_revision")
        self._require_candidate(candidate)
        frame = self._read_frame(_object(request, "data"), _object(request, "limits"))
        if task["kind"] == "model":
            self._verify_bound_artifact(_object(task, "source_artifact"))
            return self._execute_model(request, request_id, task, candidate, frame)
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

    def _execute_model(
        self,
        request: dict[str, Any],
        request_id: str,
        task: dict[str, Any],
        candidate: dict[str, Any],
        frame: pd.DataFrame,
    ) -> dict[str, Any]:
        features: dict[str, pd.Series] = {}
        factor_refs: list[dict[str, Any]] = []
        for feature in task["features"]:
            factor = dict(feature["factor_revision"])
            self._require_candidate(factor)
            factor_refs.append(factor)
            features[str(feature["feature_name"])] = _factor_values(frame, feature["calculation"])
        design = pd.DataFrame(features)
        label_field = str(task["label"]["field"])
        design["__label__"] = pd.to_numeric(frame[label_field], errors="coerce")
        timestamps = pd.to_datetime(
            frame[str(request["data"]["timestamp_column"])], utc=True, errors="raise"
        )
        windows = task["windows"]
        masks = {
            name: (timestamps >= pd.Timestamp(window["start"], tz="UTC"))
            & (timestamps <= pd.Timestamp(window["end"], tz="UTC"))
            for name, window in windows.items()
        }
        complete = design.dropna()
        train_index = complete.index.intersection(design.index[masks["train"]])
        validation_index = complete.index.intersection(design.index[masks["validation"]])
        test_index = complete.index.intersection(design.index[masks["test"]])
        if len(train_index) < 2 or not len(validation_index) or not len(test_index):
            raise CandidateDiscoveryError("Model windows contain insufficient complete samples")
        feature_names = tuple(features)
        hyperparameters = task["estimator"]["hyperparameters"]
        estimator = Ridge(
            alpha=float(hyperparameters["alpha"]),
            fit_intercept=bool(hyperparameters["fit_intercept"]),
            solver="svd",
        )
        estimator.fit(design.loc[train_index, feature_names], design.loc[train_index, "__label__"])
        validation_predictions = estimator.predict(design.loc[validation_index, feature_names])
        test_predictions = estimator.predict(design.loc[test_index, feature_names])
        metrics = {
            "train_rows": int(len(train_index)),
            "validation_rows": int(len(validation_index)),
            "test_rows": int(len(test_index)),
            "validation_mse": float(
                mean_squared_error(
                    design.loc[validation_index, "__label__"], validation_predictions
                )
            ),
            "test_mse": float(
                mean_squared_error(design.loc[test_index, "__label__"], test_predictions)
            ),
            "test_r2": float(r2_score(design.loc[test_index, "__label__"], test_predictions)),
        }
        if any(isinstance(value, float) and not math.isfinite(value) for value in metrics.values()):
            raise CandidateDiscoveryError("Model discovery produced a non-finite metric")
        model_payload = {
            "schema": "quant-runtime.ridge-model.v1",
            "request_id": request_id,
            "factor_revisions": factor_refs,
            "feature_names": list(feature_names),
            "label": task["label"],
            "windows": task["windows"],
            "fit_timestamp": task["fit_timestamp"],
            "estimator": task["estimator"],
            "seeds": task["seeds"],
            "training_environment": task["training_environment"],
            "coef": [float(value) for value in estimator.coef_],
            "intercept": float(estimator.intercept_),
        }
        model_buffer = io.BytesIO()
        joblib.dump(model_payload, model_buffer, compress=0, protocol=5)
        model_bytes = model_buffer.getvalue()
        prediction_rows = pd.DataFrame(
            {
                "row": [*validation_index, *test_index],
                "split": ["validation"] * len(validation_index) + ["test"] * len(test_index),
                "prediction": [*validation_predictions, *test_predictions],
            }
        )
        prediction_bytes = prediction_rows.to_csv(
            index=False, lineterminator="\n", float_format="%.12g"
        ).encode("utf-8")
        observed_environment = {
            "backend_id": "qlib",
            "adapter_version": "candidate-discovery.v1",
            "engine_version": qlib.__version__,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "dependency_lock_sha256": CANDIDATE_DISCOVERY_LOCK_SHA256,
        }
        manifest = {
            "schema": "quant-runtime.candidate-discovery-artifact-manifest.v1",
            "request_id": request_id,
            "task_kind": "model",
            "candidate_revision": candidate,
            "factor_revisions": factor_refs,
            "input_artifact_sha256": request["data"]["artifact"]["sha256"],
            "model_sha256": sha256_bytes(model_bytes),
            "prediction_sha256": sha256_bytes(prediction_bytes),
            "metrics": metrics,
            "environment": observed_environment,
        }
        manifest_bytes = canonical_json(manifest) + b"\n"
        artifact_specs = (
            {
                "source": model_bytes,
                "media_type": "application/octet-stream",
                "record_schema": "quant-runtime.ridge-model.v1",
                "logical_role": "trained-model",
                "name": "model.joblib",
            },
            {
                "source": prediction_bytes,
                "media_type": "text/csv",
                "record_schema": "quant-runtime.model-predictions.v1",
                "logical_role": "candidate-discovery-output",
                "name": "predictions.csv",
            },
            {
                "source": manifest_bytes,
                "media_type": "application/json",
                "record_schema": manifest["schema"],
                "logical_role": "candidate-discovery-manifest",
                "name": "discovery-manifest.json",
            },
        )
        if (
            sum(len(item["source"]) for item in artifact_specs)
            > request["limits"]["max_output_bytes"]
        ):
            raise CandidateDiscoveryError("candidate discovery output exceeds the byte limit")
        payload = {
            "schema": RECORD_TYPE,
            "status": "completed",
            "evidence_level": "discovery-only",
            "request_id": request_id,
            "task_kind": "model",
            "candidate_revision": candidate,
            "factor_revisions": factor_refs,
            "data": request["data"],
            "label": task["label"],
            "windows": task["windows"],
            "fit_timestamp": task["fit_timestamp"],
            "estimator": task["estimator"],
            "seeds": task["seeds"],
            "training_environment": task["training_environment"],
            "environment": observed_environment,
            "metrics": metrics,
            "artifact_digests": sorted(sha256_bytes(item["source"]) for item in artifact_specs),
        }
        record_id = sha256_value(payload)
        lineage = [
            {
                "source_kind": item["record_type"],
                "source_id": item["record_id"],
                "relation": "evaluates-candidate" if item is candidate else "uses-factor-revision",
            }
            for item in [candidate, *factor_refs]
        ]
        publication = {
            "record_id": record_id,
            "record_type": RECORD_TYPE,
            "payload": payload,
            "lineage": lineage,
        }
        try:
            current = self._workspace.get_record(record_id)
        except Exception as exc:
            if getattr(exc, "code", None) != "record_not_found" and not isinstance(exc, KeyError):
                raise
            current = self._workspace.publish_record(publication, artifacts=artifact_specs)
        if (
            current.get("record_id") != record_id
            or current.get("record_type") != RECORD_TYPE
            or current.get("payload") != payload
            or current.get("lineage") != lineage
        ):
            raise CandidateDiscoveryError("candidate discovery publication identity conflict")
        published_artifacts = current.get("artifacts")
        if not isinstance(published_artifacts, list) or len(published_artifacts) != 3:
            raise CandidateDiscoveryError("candidate discovery artifacts are incomplete")
        if (
            sorted(str(item.get("sha256", "")) for item in published_artifacts)
            != payload["artifact_digests"]
        ):
            raise CandidateDiscoveryError("candidate discovery artifact identity mismatch")
        for artifact in published_artifacts:
            if (
                self._workspace.verify_artifact(str(artifact["uri"])).get("sha256")
                != artifact["sha256"]
            ):
                raise CandidateDiscoveryError("candidate discovery artifact verification failed")
        if self._workspace.get_record(record_id) != current:
            raise CandidateDiscoveryError("candidate discovery canonical readback mismatch")
        return {
            "schema": RESULT_SCHEMA,
            "status": "completed",
            "evidence_level": "discovery-only",
            "request_id": request_id,
            "task_kind": "model",
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

    def _verify_bound_artifact(self, artifact: Mapping[str, Any]) -> None:
        verification = self._workspace.verify_artifact(str(artifact["uri"]))
        if verification.get("sha256") != artifact["sha256"]:
            raise CandidateDiscoveryError("Model source artifact verification failed")

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
    if task.get("kind") == "factor":
        _validate_factor_task(task)
    elif task.get("kind") == "model":
        _validate_model_task(task)
    else:
        raise CandidateDiscoveryError("candidate discovery task kind is invalid")
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


def _validate_artifact(artifact: Mapping[str, Any], label: str) -> None:
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
        label,
    )
    digest = _sha(artifact["sha256"], label)
    if (
        artifact["schema"] != "quant-research.artifact-ref.v1"
        or artifact["uri"] != f"workspace-artifact://sha256/{digest}"
        or not isinstance(artifact["bytes"], int)
        or artifact["bytes"] < 1
    ):
        raise CandidateDiscoveryError(f"{label} is invalid")
    for name in ("media_type", "logical_role", "name"):
        _bounded(artifact[name], f"{label} {name}")
    if artifact["record_schema"] is not None:
        _bounded(artifact["record_schema"], f"{label} record schema")


def _validate_factor_task(task: Mapping[str, Any]) -> None:
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
                "field_id",
                "capability",
                "column",
                "frequency",
                "adjustment",
                "as_of",
                "lag_bars",
                "unit",
                "null_policy",
                "required_semantics",
            },
            "Factor input",
        )
        name = _bounded(current["name"], "Factor input name")
        field_id = _bounded(current["field_id"], "Factor field identity")
        _bounded(current["capability"], "Factor capability")
        column = _bounded(current["column"], "Factor input column")
        if name in names or field_id.startswith("__") or column.startswith("__"):
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
        _validate_required_semantics(current["required_semantics"])
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


def _validate_model_task(task: Mapping[str, Any]) -> None:
    _keys(
        task,
        {
            "kind",
            "candidate_revision",
            "features",
            "label",
            "windows",
            "fit_timestamp",
            "estimator",
            "seeds",
            "training_environment",
            "source_artifact",
        },
        "Model task",
    )
    candidate = _object(task, "candidate_revision")
    _keys(candidate, {"record_id", "record_type", "semantic_id"}, "candidate")
    _sha(candidate["record_id"], "candidate record")
    _sha(candidate["semantic_id"], "candidate semantic")
    if candidate["record_type"] != "apex-research.model-candidate.v1":
        raise CandidateDiscoveryError("Model task candidate type is invalid")
    features = task["features"]
    if not isinstance(features, list) or not features or len(features) > 256:
        raise CandidateDiscoveryError("Model features are invalid")
    names: set[str] = set()
    factors: set[str] = set()
    for item in features:
        if not isinstance(item, Mapping):
            raise CandidateDiscoveryError("Model feature must be an object")
        feature = dict(item)
        _keys(feature, {"feature_name", "factor_revision", "calculation"}, "Model feature")
        name = _bounded(feature["feature_name"], "Model feature name")
        factor = _object(feature, "factor_revision")
        _keys(factor, {"record_id", "record_type", "semantic_id"}, "Factor revision")
        _sha(factor["record_id"], "Factor revision")
        _sha(factor["semantic_id"], "Factor semantic")
        if factor["record_type"] != "apex-research.factor-candidate.v1":
            raise CandidateDiscoveryError("Model feature Factor type is invalid")
        if name in names or factor["record_id"] in factors:
            raise CandidateDiscoveryError("Model features must be unique and canonical")
        names.add(name)
        factors.add(str(factor["record_id"]))
        calculation = _object(feature, "calculation")
        _validate_factor_calculation(calculation)
    label = _object(task, "label")
    _keys(
        label,
        {"registry", "field", "kind", "horizon", "frequency", "unit"},
        "Model label",
    )
    _bounded(label["field"], "Model label field")
    if (
        label["registry"] != "apex-research.label-registry.v1"
        or label["kind"] not in {"forward_return", "forward_excess_return", "direction"}
        or not isinstance(label["horizon"], int)
        or not 1 <= label["horizon"] <= 10_000
        or label["frequency"] not in {"1d", "1m"}
        or not _bounded(label["unit"], "Model label unit")
    ):
        raise CandidateDiscoveryError("Model label is unsupported")
    windows = _object(task, "windows")
    _keys(windows, {"train", "validation", "test"}, "Model windows")
    normalized_windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for name in ("train", "validation", "test"):
        window = _object(windows, name)
        _keys(window, {"start", "end"}, f"Model {name} window")
        try:
            start = pd.Timestamp(_bounded(window["start"], "window start"), tz="UTC")
            end = pd.Timestamp(_bounded(window["end"], "window end"), tz="UTC")
        except Exception as exc:
            raise CandidateDiscoveryError("Model window timestamp is invalid") from exc
        if start > end:
            raise CandidateDiscoveryError("Model window is reversed")
        normalized_windows.append((start, end))
    if not (
        normalized_windows[0][1] < normalized_windows[1][0]
        and normalized_windows[1][1] < normalized_windows[2][0]
    ):
        raise CandidateDiscoveryError("Model windows overlap or leak")
    fit = _object(task, "fit_timestamp")
    _keys(fit, {"policy", "timestamp"}, "Model fit policy")
    if fit["policy"] != "after_training_window":
        raise CandidateDiscoveryError("Model fit policy is unsupported")
    try:
        fit_timestamp = pd.Timestamp(_bounded(fit["timestamp"], "fit timestamp"))
        if fit_timestamp.tzinfo is None:
            raise ValueError
        fit_timestamp = fit_timestamp.tz_convert("UTC")
    except Exception as exc:
        raise CandidateDiscoveryError("Model fit timestamp is invalid") from exc
    if not normalized_windows[0][1] < fit_timestamp <= normalized_windows[1][0]:
        raise CandidateDiscoveryError("Model fit timestamp leaks validation data")
    estimator = _object(task, "estimator")
    _keys(estimator, {"registry", "kind", "hyperparameters"}, "Model estimator")
    if (
        estimator["registry"] != "apex-research.estimator-registry.v1"
        or estimator["kind"] != "ridge"
    ):
        raise CandidateDiscoveryError("Model estimator is unsupported")
    hyperparameters = _object(estimator, "hyperparameters")
    _keys(hyperparameters, {"alpha", "fit_intercept"}, "Ridge hyperparameters")
    if _finite_number(hyperparameters["alpha"], "Ridge alpha") < 0 or not isinstance(
        hyperparameters["fit_intercept"], bool
    ):
        raise CandidateDiscoveryError("Ridge hyperparameters are invalid")
    seeds = task["seeds"]
    if not isinstance(seeds, list) or not seeds or len(seeds) > 64:
        raise CandidateDiscoveryError("Model seeds are invalid")
    previous: tuple[str, int] | None = None
    for item in seeds:
        if not isinstance(item, Mapping):
            raise CandidateDiscoveryError("Model seed must be an object")
        seed = dict(item)
        _keys(seed, {"purpose", "value"}, "Model seed")
        if seed["purpose"] not in {
            "estimator",
            "data_split",
            "feature_selection",
        } or not isinstance(seed["value"], int):
            raise CandidateDiscoveryError("Model seed is invalid")
        current = (str(seed["purpose"]), int(seed["value"]))
        if previous is not None and current <= previous:
            raise CandidateDiscoveryError("Model seeds must be unique and canonical")
        previous = current
    environment = _object(task, "training_environment")
    _keys(
        environment,
        {
            "runtime",
            "runtime_version",
            "platform",
            "dependency_lock_sha256",
            "container_image_sha256",
        },
        "training environment",
    )
    if (
        environment["runtime"] != "cpython"
        or environment["runtime_version"] != platform.python_version()
        or environment["platform"] != "portable"
        or environment["dependency_lock_sha256"] != CANDIDATE_DISCOVERY_LOCK_SHA256
        or environment["container_image_sha256"] is not None
    ):
        raise CandidateDiscoveryError("Model training environment drifted")
    _validate_artifact(_object(task, "source_artifact"), "Model source artifact")


def _validate_factor_calculation(calculation: Mapping[str, Any]) -> None:
    # The calculation contract is the Factor task minus its owner reference.
    _keys(
        calculation,
        {"expression", "inputs", "output", "warm_up", "missing_values"},
        "Factor calculation",
    )
    names: set[str] = set()
    inputs = calculation["inputs"]
    if not isinstance(inputs, list) or not inputs or len(inputs) > 64:
        raise CandidateDiscoveryError("Factor inputs are invalid")
    for item in inputs:
        if not isinstance(item, Mapping):
            raise CandidateDiscoveryError("Factor input must be an object")
        current = dict(item)
        _keys(
            current,
            {
                "name",
                "field_id",
                "capability",
                "column",
                "frequency",
                "adjustment",
                "as_of",
                "lag_bars",
                "unit",
                "null_policy",
                "required_semantics",
            },
            "Factor input",
        )
        name = _bounded(current["name"], "Factor input name")
        _bounded(current["field_id"], "Factor field identity")
        _bounded(current["capability"], "Factor capability")
        _bounded(current["column"], "Factor input column")
        if name in names:
            raise CandidateDiscoveryError("Factor input names must be unique")
        names.add(name)
        if (
            current["frequency"] not in {"1d", "1m"}
            or current["adjustment"] not in {"none", "forward", "backward"}
            or current["as_of"] not in {"decision_time", "prior_close"}
            or not isinstance(current["lag_bars"], int)
            or not 0 <= current["lag_bars"] <= 10_000
            or current["null_policy"] not in {"reject", "allow"}
        ):
            raise CandidateDiscoveryError("Factor input semantics are unsupported")
        _validate_required_semantics(current["required_semantics"])
    _validate_expression(calculation["expression"], names, 0)
    output = _object(calculation, "output")
    _keys(output, {"dtype", "unit"}, "Factor output")
    if output["dtype"] != "float64":
        raise CandidateDiscoveryError("Factor output dtype is unsupported")
    _bounded(output["unit"], "Factor output unit")
    warm_up = _object(calculation, "warm_up")
    _keys(warm_up, {"periods", "behavior"}, "Factor warm-up")
    if (
        not isinstance(warm_up["periods"], int)
        or not 0 <= warm_up["periods"] <= 100_000
        or warm_up["behavior"] not in {"emit_null", "reject"}
        or calculation["missing_values"] not in {"propagate", "reject"}
    ):
        raise CandidateDiscoveryError("Factor calculation missing-data semantics are unsupported")


def _factor_values(frame: pd.DataFrame, task: Mapping[str, Any]) -> pd.Series:
    sources: dict[str, pd.Series] = {}
    for item in task["inputs"]:
        field = str(item["column"])
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


def _validate_required_semantics(value: object) -> None:
    if not isinstance(value, list) or len(value) > 16:
        raise CandidateDiscoveryError("Factor required semantics are invalid")
    dimensions: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CandidateDiscoveryError("Factor required semantic must be an object")
        current = dict(item)
        _keys(current, {"dimension", "required"}, "Factor required semantic")
        dimension = _bounded(current["dimension"], "Factor semantic dimension")
        if dimension not in {
            "field_availability",
            "point_in_time",
            "time",
            "provider_lineage",
        } or not isinstance(current["required"], bool):
            raise CandidateDiscoveryError("Factor required semantic is unsupported")
        dimensions.append(dimension)
    if dimensions != sorted(set(dimensions)):
        raise CandidateDiscoveryError("Factor required semantics must be unique and canonical")


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
