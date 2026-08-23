from quant_runtime.sdk.capability_contract import AdapterRegistry, CapabilityProfile


def test_capability_matching_is_deterministic_at_registry_scale() -> None:
    registry = AdapterRegistry()
    names = [f"backend-{index:04d}" for index in range(1000)]
    for name in reversed(names):
        registry.register(
            CapabilityProfile.from_dict(
                {
                    "schema": "quant-research.runtime-capability.v1",
                    "backend_id": name,
                    "role": "formal",
                    "adapter_version": "test",
                    "engine_version": "test",
                    "provides": ["scale.required"],
                }
            ),
            object,
        )
    selected = registry.resolve_formal(
        {"mode": "capability_match", "backend_preference": [names[-1]]},
        {"scale.required"},
        names,
    )
    assert selected.backend_ids == (names[-1],)
