from __future__ import annotations

import math
from itertools import combinations
from typing import Any

from quant_runtime.adapters.interface import FormalAdapterResult


def compare_results(
    topology: str,
    results: tuple[FormalAdapterResult, ...],
    *,
    agreement: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if topology not in {"formal_comparison", "agreement_gate"}:
        return None
    absolute_tolerance, relative_tolerance = _tolerances(agreement or {})
    selected = tuple(str(item) for item in (agreement or {}).get("selectors", ()))
    pairwise = []
    for left, right in combinations(results, 2):
        common = sorted(set(left.metrics) & set(right.metrics))
        compared = list(selected) if selected else common
        missing = [key for key in compared if key not in left.metrics or key not in right.metrics]
        if missing:
            raise ValueError(f"comparison selectors are missing: {missing}")
        differing = [
            key
            for key in compared
            if not _equivalent(
                left.metrics[key],
                right.metrics[key],
                absolute_tolerance,
                relative_tolerance,
            )
        ]
        pairwise.append(
            {
                "formal_ids": [left.formal_id, right.formal_id],
                "adapters": [left.backend_id, right.backend_id],
                "common_metric_count": len(common),
                "compared_metrics": compared,
                "differing_metrics": differing,
                "status": "matched" if not differing else "different",
            }
        )
    value: dict[str, Any] = {
        "topology": topology,
        "status": "completed",
        "formal_ids": [item.formal_id for item in results],
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "pairwise": pairwise,
    }
    if topology == "agreement_gate":
        policy = agreement or {}
        selectors = _selector_agreement(
            results,
            selected,
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
        )
        require_all = bool(policy.get("require_all", True))
        decisions = [item["agreed"] for item in selectors.values()]
        passed = all(decisions) if require_all else any(decisions)
        value.update(
            {
                "status": "passed" if passed else "rejected",
                "gate": {
                    "passed": passed,
                    "require_all": require_all,
                    "selectors": selectors,
                },
            }
        )
    return value


def _tolerances(policy: dict[str, Any]) -> tuple[float, float]:
    absolute = float(policy.get("absolute_tolerance", 0.0))
    relative = float(policy.get("relative_tolerance", 0.0))
    if any(not math.isfinite(value) or value < 0 for value in (absolute, relative)):
        raise ValueError("agreement tolerances must be finite and non-negative")
    return absolute, relative


def _selector_agreement(
    results: tuple[FormalAdapterResult, ...],
    selectors: tuple[str, ...],
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> dict[str, dict[str, Any]]:
    if not selectors:
        raise ValueError("agreement_gate requires at least one selector")
    output: dict[str, dict[str, Any]] = {}
    for selector in selectors:
        values = []
        for result in results:
            if selector not in result.metrics:
                raise ValueError(f"agreement metric is missing: {selector!r}")
            values.append((result.formal_id, result.metrics[selector]))
        agreed = all(
            _equivalent(left[1], right[1], absolute_tolerance, relative_tolerance)
            for left, right in combinations(values, 2)
        )
        output[selector] = {
            "agreed": agreed,
            "values": {formal_id: value for formal_id, value in values},
        }
    return output


def _equivalent(
    left: Any,
    right: Any,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> bool:
    left = _stable(left)
    right = _stable(right)
    if isinstance(left, int | float) and isinstance(right, int | float):
        difference = abs(float(left) - float(right))
        scale = max(abs(float(left)), abs(float(right)))
        return difference <= max(absolute_tolerance, relative_tolerance * scale)
    return left == right


def _stable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite formal metric is incomparable: {value!r}")
    return value
