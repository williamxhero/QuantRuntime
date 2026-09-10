from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from quant_runtime.artifacts import sha256_value


class SandboxPolicyError(ValueError):
    """A sandbox request cannot satisfy the immutable Runtime policy."""


@dataclass(frozen=True, slots=True)
class ResolvedSandboxPolicy:
    package_hash: str
    profile: dict[str, Any]
    identity_hash: str

    @property
    def execution_mode(self) -> str:
        return str(self.profile["execution_mode"])


class SandboxPolicyRegistry:
    """Resolve caller input against Runtime-owned immutable trust decisions."""

    def __init__(self, *, direct_package_hashes: frozenset[str] = frozenset()) -> None:
        self._direct_package_hashes = direct_package_hashes

    def resolve(
        self, package_record: Mapping[str, Any], profile: Mapping[str, Any]
    ) -> ResolvedSandboxPolicy:
        package_ref = _object(package_record, "package_ref")
        manifest = _object(package_record, "manifest")
        value = _profile(profile)
        package_hash = str(package_ref.get("package_hash", ""))
        if len(package_hash) != 64:
            raise SandboxPolicyError("package identity is invalid")
        generated = manifest.get("schema") == "quant-research.strategy-package.v2"
        if generated and (
            value["execution_mode"] != "isolated"
            or value["trust_classification"] != "generated_untrusted"
        ):
            raise SandboxPolicyError("generated packages are always untrusted")
        if not generated and value["execution_mode"] == "direct":
            if value["trust_classification"] != "human_allowlisted":
                raise SandboxPolicyError("direct execution requires human allowlisted trust")
            if package_hash not in self._direct_package_hashes:
                raise SandboxPolicyError("human package identity is not allowlisted")
        if (
            not generated
            and value["execution_mode"] == "isolated"
            and value["trust_classification"] != "human_isolated"
        ):
            raise SandboxPolicyError("isolated human package has invalid trust classification")
        return ResolvedSandboxPolicy(package_hash, value, sha256_value(value))


def _profile(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = {str(key): item for key, item in value.items()}
    required = {
        "schema",
        "profile_id",
        "revision",
        "execution_mode",
        "trust_classification",
        "containment",
        "dependency_environment",
        "capabilities",
        "limits",
    }
    if set(profile) != required or profile.get("schema") != "quant-runtime.sandbox-profile.v1":
        raise SandboxPolicyError("sandbox profile shape is invalid")
    if profile["execution_mode"] not in {"isolated", "direct"}:
        raise SandboxPolicyError("sandbox execution mode is invalid")
    for name in ("containment", "dependency_environment", "capabilities", "limits"):
        if not isinstance(profile[name], Mapping):
            raise SandboxPolicyError(f"sandbox profile {name} must be an object")
        profile[name] = {str(key): item for key, item in profile[name].items()}
    limits = profile["limits"]
    if set(limits) != {
        "cpu_seconds",
        "memory_bytes",
        "wall_clock_seconds",
        "processes",
        "filesystem_bytes",
        "stdout_bytes",
        "stderr_bytes",
        "artifacts",
    } or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in limits.values()
    ):
        raise SandboxPolicyError("sandbox limits are invalid")
    if profile["capabilities"].get("network") != "deny":
        raise SandboxPolicyError("sandbox network capability must default to deny")
    return profile


def _object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise SandboxPolicyError(f"package record lacks {name}")
    return {str(key): member for key, member in item.items()}
