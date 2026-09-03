from __future__ import annotations

from copy import deepcopy

import pytest

from quant_runtime.sandbox import SandboxPolicyError, SandboxPolicyRegistry


def package_record(*, generated: bool, package_hash: str = "a" * 64) -> dict:
    return {
        "package_ref": {
            "schema": "quant-research.strategy-package-ref.v1",
            "strategy_id": "generated.test" if generated else "human.test",
            "revision": 1,
            "package_hash": package_hash,
        },
        "manifest": {
            "schema": (
                "quant-research.strategy-package.v2"
                if generated
                else "quant-research.strategy-package.v1"
            )
        },
    }


def isolated_profile() -> dict:
    return {
        "schema": "quant-runtime.sandbox-profile.v1",
        "profile_id": "generated-default",
        "revision": 1,
        "execution_mode": "isolated",
        "trust_classification": "generated_untrusted",
        "containment": {
            "backend_id": "oci-linux",
            "mechanism": "linux-oci-container",
            "mechanism_version": "1",
            "platform": "linux",
            "proof": "sha256:" + "b" * 64,
        },
        "dependency_environment": {
            "kind": "oci-image",
            "identity": "sha256:" + "c" * 64,
        },
        "capabilities": {"network": "deny", "filesystem": "sealed", "subprocess": "deny"},
        "limits": {
            "cpu_seconds": 10,
            "memory_bytes": 268435456,
            "wall_clock_seconds": 20,
            "processes": 8,
            "filesystem_bytes": 10485760,
            "stdout_bytes": 65536,
            "stderr_bytes": 65536,
            "artifacts": 32,
        },
    }


def test_generated_package_can_never_select_direct_execution() -> None:
    profile = isolated_profile()
    profile["execution_mode"] = "direct"
    profile["trust_classification"] = "human_allowlisted"

    with pytest.raises(SandboxPolicyError, match="generated packages are always untrusted"):
        SandboxPolicyRegistry().resolve(package_record(generated=True), profile)


def test_human_direct_execution_requires_exact_registry_identity() -> None:
    record = package_record(generated=False)
    profile = deepcopy(isolated_profile())
    profile["profile_id"] = "human-direct"
    profile["execution_mode"] = "direct"
    profile["trust_classification"] = "human_allowlisted"

    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        SandboxPolicyRegistry().resolve(record, profile)

    resolved = SandboxPolicyRegistry(
        direct_package_hashes=frozenset({record["package_ref"]["package_hash"]})
    ).resolve(record, profile)
    assert resolved.execution_mode == "direct"
    assert resolved.package_hash == record["package_ref"]["package_hash"]
    assert resolved.identity_hash


def test_profile_identity_changes_for_every_meaning_bearing_limit() -> None:
    record = package_record(generated=True)
    first = SandboxPolicyRegistry().resolve(record, isolated_profile())
    changed = isolated_profile()
    changed["limits"]["memory_bytes"] += 1
    second = SandboxPolicyRegistry().resolve(record, changed)

    assert first.identity_hash != second.identity_hash
