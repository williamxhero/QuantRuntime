from __future__ import annotations

from dataclasses import fields

import pytest

from quant_runtime.adapters.interface import FormalRunInput
from quant_runtime.workspace.comparison import compare_results


def test_formal_input_cannot_receive_a_qlib_candidate() -> None:
    names = {item.name for item in fields(FormalRunInput)}
    assert "candidate" not in names
    assert "discovery" not in names
    assert names == {
        "package",
        "parameters",
        "snapshot",
        "output",
        "config",
        "cache_path",
        "cache_policy",
        "cache_transform_version",
    }


def test_comparison_is_a_post_formal_operation_without_reference_backend() -> None:
    from quant_runtime.adapters.interface import FormalAdapterResult

    results = tuple(
        FormalAdapterResult(
            backend_id=name,
            adapter_version="test",
            engine_version="test",
            status="completed",
            metrics={"return": value},
            positions=(),
            fills=(),
            account_curve=(),
            native_evidence=(),
        )
        for name, value in (("one", 1.0), ("two", 2.0))
    )
    compared = compare_results("comparison", results)
    assert compared is not None
    assert compared["reference_backend"] is None
    assert compared["pairwise"][0]["differing_metrics"] == ["return"]
    gated = compare_results(
        "agreement_gate",
        results,
        agreement={
            "conclusions": [{"name": "positive", "metric": "return", "operator": "positive"}]
        },
        minimum_agreement=2,
    )
    assert gated is not None and gated["passed"] is True


def test_non_finite_comparison_metric_fails_closed() -> None:
    from quant_runtime.adapters.interface import FormalAdapterResult

    def result(name, value):
        return FormalAdapterResult(
            backend_id=name,
            adapter_version="test",
            engine_version="test",
            status="completed",
            metrics={"return": value},
            positions=(),
            fills=(),
            account_curve=(),
            native_evidence=(),
        )

    with pytest.raises(ValueError, match="incomparable"):
        compare_results("comparison", (result("one", float("nan")), result("two", 1.0)))
