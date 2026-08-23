from __future__ import annotations

import pytest

from quant_runtime.sdk.capability_contract import (
    AdapterRegistry,
    CapabilityError,
    CapabilityProfile,
)


def profile(name: str, *capabilities: str) -> CapabilityProfile:
    return CapabilityProfile.from_dict(
        {
            "schema": "quant-research.runtime-capability.v1",
            "backend_id": name,
            "role": "formal",
            "adapter_version": "test",
            "engine_version": "test",
            "provides": list(capabilities),
        }
    )


def registry() -> AdapterRegistry:
    value = AdapterRegistry()
    value.register(profile("zeta", "required"), object)
    value.register(profile("alpha", "required"), object)
    return value


def test_capability_match_never_uses_registration_or_sort_order_as_fallback() -> None:
    value = registry()
    with pytest.raises(CapabilityError, match="explicit backend_preference"):
        value.resolve_formal(
            {"mode": "capability_match"},
            {"required"},
            {"alpha", "zeta"},
        )
    selected = value.resolve_formal(
        {"mode": "capability_match", "backend_preference": ["zeta", "alpha"]},
        {"required"},
        {"alpha", "zeta"},
    )
    assert selected.backend_ids == ("zeta",)


def test_all_modes_fail_closed_on_missing_capability_or_implementation() -> None:
    value = registry()
    with pytest.raises(CapabilityError, match="lacks required"):
        value.resolve_formal(
            {"mode": "pinned", "backend": "alpha"},
            {"missing"},
            {"alpha"},
        )
    with pytest.raises(CapabilityError, match="no formal implementation"):
        value.resolve_formal(
            {"mode": "pinned", "backend": "alpha"},
            {"required"},
            {"zeta"},
        )


def test_comparison_and_agreement_preserve_explicit_backend_order() -> None:
    value = registry()
    comparison = value.resolve_formal(
        {"mode": "comparison", "backends": ["zeta", "alpha"]},
        {"required"},
        {"alpha", "zeta"},
    )
    assert comparison.backend_ids == ("zeta", "alpha")
    agreement = value.resolve_formal(
        {
            "mode": "agreement_gate",
            "backends": ["alpha", "zeta"],
            "minimum_agreement": 2,
        },
        {"required"},
        {"alpha", "zeta"},
    )
    assert agreement.backend_ids == ("alpha", "zeta")
    assert agreement.minimum_agreement == 2
