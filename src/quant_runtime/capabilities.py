from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class CapabilityError(ValueError):
    """A requested topology cannot be executed without semantic degradation."""


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    backend_id: str
    role: str
    adapter_version: str
    engine_version: str
    provides: frozenset[str]
    conditional: dict[str, str]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityProfile:
        required = {"backend_id", "role", "adapter_version", "engine_version", "provides"}
        if missing := required - value.keys():
            raise ValueError(f"capability profile lacks fields: {sorted(missing)}")
        role = str(value["role"])
        if role not in {"data", "discovery", "formal"}:
            raise ValueError(f"invalid capability role {role!r}")
        provided = value["provides"]
        if not isinstance(provided, list):
            raise ValueError("capability profile provides must be a list")
        raw_conditional = value.get("conditional", {})
        if not isinstance(raw_conditional, Mapping):
            raise ValueError("capability profile conditional must be an object")
        conditional = {
            str(name): str(item["policy"])
            for name, item in raw_conditional.items()
            if isinstance(item, Mapping) and "policy" in item
        }
        return cls(
            backend_id=str(value["backend_id"]),
            role=role,
            adapter_version=str(value["adapter_version"]),
            engine_version=str(value["engine_version"]),
            provides=frozenset(str(item) for item in provided),
            conditional=conditional,
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.provides | frozenset(self.conditional)

    def missing(self, required: Iterable[str]) -> frozenset[str]:
        return frozenset(required) - self.capabilities

    def identity(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "role": self.role,
            "adapter_version": self.adapter_version,
            "engine_version": self.engine_version,
            "provides": sorted(self.provides),
            "conditional": dict(sorted(self.conditional.items())),
        }


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    profile: CapabilityProfile
    factory: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class FormalExecution:
    formal_id: str
    adapter: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    topology: str
    discovery_adapter: str | None
    discovery_config: dict[str, Any]
    formal: tuple[FormalExecution, ...]
    agreement: dict[str, Any] | None


class AdapterRegistry:
    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], AdapterRegistration] = {}

    def register(self, profile: CapabilityProfile, factory: Callable[[], Any]) -> None:
        key = profile.role, profile.backend_id
        if key in self._registrations:
            raise ValueError(f"adapter already registered: {profile.role}/{profile.backend_id}")
        self._registrations[key] = AdapterRegistration(profile, factory)

    def names(self, role: str) -> tuple[str, ...]:
        return tuple(sorted(key[1] for key in self._registrations if key[0] == role))

    def profile(self, role: str, backend_id: str) -> CapabilityProfile:
        try:
            return self._registrations[(role, backend_id)].profile
        except KeyError as exc:
            raise CapabilityError(f"unregistered {role} adapter {backend_id!r}") from exc

    def create(self, role: str, backend_id: str) -> Any:
        self.profile(role, backend_id)
        return self._registrations[(role, backend_id)].factory()

    def resolve_plan(
        self,
        execution: Mapping[str, Any],
        *,
        required: Iterable[str],
        discovery_policy: str,
        discovery_implementations: Iterable[str],
        formal_implementations: Iterable[str],
    ) -> ExecutionPlan:
        topology = str(execution.get("topology", ""))
        if topology not in {
            "formal_only",
            "discovery_formal",
            "formal_comparison",
            "agreement_gate",
        }:
            raise CapabilityError(f"unsupported execution topology {topology!r}")

        discovery = execution.get("discovery")
        discovery_adapter: str | None = None
        discovery_config: dict[str, Any] = {}
        if discovery is not None:
            if not isinstance(discovery, Mapping):
                raise CapabilityError("execution.discovery must be an object")
            discovery_adapter = str(discovery.get("adapter", ""))
            raw_config = discovery.get("config", {})
            if not discovery_adapter or not isinstance(raw_config, Mapping):
                raise CapabilityError("discovery requires adapter and object config")
            discovery_config = dict(raw_config)
        requires_discovery = topology == "discovery_formal"
        if requires_discovery and discovery_adapter is None:
            raise CapabilityError("discovery_formal requires execution.discovery")
        if topology != "discovery_formal" and discovery_adapter is not None:
            raise CapabilityError(f"{topology} does not accept a discovery stage")
        if discovery_policy == "required" and discovery_adapter is None:
            raise CapabilityError("strategy package requires discovery")
        if discovery_policy == "forbidden" and discovery_adapter is not None:
            raise CapabilityError("strategy package forbids discovery")
        if discovery_adapter is not None:
            if discovery_adapter not in set(discovery_implementations):
                raise CapabilityError(
                    f"strategy package has no discovery implementation for {discovery_adapter!r}"
                )
            self.profile("discovery", discovery_adapter)

        raw_formal = execution.get("formal")
        if not isinstance(raw_formal, list) or not raw_formal:
            raise CapabilityError("execution.formal must be a non-empty list")
        formal: list[FormalExecution] = []
        seen_ids: set[str] = set()
        available = set(formal_implementations)
        for item in raw_formal:
            if not isinstance(item, Mapping):
                raise CapabilityError("each formal execution must be an object")
            formal_id = str(item.get("id", ""))
            adapter = str(item.get("adapter", ""))
            config = item.get("config", {})
            if not formal_id or formal_id in seen_ids:
                raise CapabilityError("formal execution ids must be non-empty and unique")
            if adapter not in available:
                raise CapabilityError(
                    f"strategy package has no formal implementation for {adapter!r}"
                )
            if not isinstance(config, Mapping):
                raise CapabilityError("formal config must be an object")
            missing = self.profile("formal", adapter).missing(required)
            if missing:
                raise CapabilityError(
                    f"formal adapter {adapter!r} lacks required capabilities: {sorted(missing)}"
                )
            seen_ids.add(formal_id)
            formal.append(FormalExecution(formal_id, adapter, dict(config)))

        comparison = topology in {"formal_comparison", "agreement_gate"}
        if comparison != (len(formal) >= 2):
            expected = "at least two" if comparison else "exactly one"
            raise CapabilityError(f"{topology} requires {expected} formal executions")
        agreement = execution.get("agreement")
        if topology == "agreement_gate":
            if not isinstance(agreement, Mapping):
                raise CapabilityError("agreement_gate requires execution.agreement")
            agreement_value: dict[str, Any] | None = dict(agreement)
        elif agreement is not None:
            raise CapabilityError(f"{topology} does not accept agreement policy")
        else:
            agreement_value = None
        return ExecutionPlan(
            topology=topology,
            discovery_adapter=discovery_adapter,
            discovery_config=discovery_config,
            formal=tuple(formal),
            agreement=agreement_value,
        )
