from __future__ import annotations

from itertools import combinations
from typing import Any

from quant_runtime.adapters.interface import FormalAdapterResult


def compare_results(
    mode: str,
    results: tuple[FormalAdapterResult, ...],
    *,
    agreement: dict[str, Any] | None = None,
    minimum_agreement: int | None = None,
) -> dict[str, Any] | None:
    if mode not in {"comparison", "agreement_gate"}:
        return None
    pairwise = []
    for left, right in combinations(results, 2):
        common = sorted(set(left.metrics) & set(right.metrics))
        differing = [
            key
            for key in common
            if _stable_metric(left.metrics[key]) != _stable_metric(right.metrics[key])
        ]
        pairwise.append(
            {
                "backends": [left.backend_id, right.backend_id],
                "differing_metrics": differing,
            }
        )
    value: dict[str, Any] = {
        "mode": mode,
        "backends": [item.backend_id for item in results],
        "reference_backend": None,
        "pairwise": pairwise,
    }
    if mode == "agreement_gate":
        conclusions = _conclusions(results, agreement or {})
        agreed = sum(all(row.values()) for row in conclusions.values())
        value.update(
            {
                "minimum_agreement": minimum_agreement,
                "agreed_backends": agreed,
                "passed": agreed >= int(minimum_agreement or 0),
                "conclusions": conclusions,
            }
        )
    return value


def _conclusions(
    results: tuple[FormalAdapterResult, ...],
    policy: dict[str, Any],
) -> dict[str, dict[str, bool]]:
    rules = policy.get("conclusions", [])
    if not rules:
        return {item.backend_id: {"completed": item.status == "completed"} for item in results}
    if not isinstance(rules, list) or any(not isinstance(item, dict) for item in rules):
        raise ValueError("agreement conclusions must be a list of objects")
    output: dict[str, dict[str, bool]] = {}
    for result in results:
        values = {}
        for rule in rules:
            name = str(rule.get("name", rule.get("metric", "")))
            metric = str(rule.get("metric", ""))
            if not name or metric not in result.metrics:
                raise ValueError(f"agreement metric is missing: {metric!r}")
            values[name] = _compare(
                result.metrics[metric],
                str(rule.get("operator", "eq")),
                rule.get("threshold"),
            )
        output[result.backend_id] = values
    return output


def _compare(value: Any, operator: str, threshold: Any) -> bool:
    if operator == "eq":
        return value == threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lte":
        return value <= threshold
    if operator == "positive":
        return value > 0
    if operator == "negative":
        return value < 0
    raise ValueError(f"unsupported agreement operator {operator!r}")


def _stable_metric(value: Any) -> Any:
    return None if isinstance(value, float) else value
