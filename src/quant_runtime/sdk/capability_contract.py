from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .schema import validate_instance


class CapabilityError(ValueError):
    """A requested topology cannot be satisfied without semantic degradation."""


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    backend_id: str
    role: str
    adapter_version: str
    engine_version: str
    provides: frozenset[str]
    conditional: dict[str, str]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapabilityProfile:
        validate_instance("runtime-capability.v1", value)
        conditional = {
            str(name): str(item["policy"]) for name, item in value.get("conditional", {}).items()
        }
        return cls(
            backend_id=str(value["backend_id"]),
            role=str(value["role"]),
            adapter_version=str(value["adapter_version"]),
            engine_version=str(value["engine_version"]),
            provides=frozenset(str(item) for item in value["provides"]),
            conditional=conditional,
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.provides | frozenset(self.conditional)

    def missing(self, required: Iterable[str]) -> frozenset[str]:
        return frozenset(required) - self.capabilities


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    profile: CapabilityProfile
    factory: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class FormalSelection:
    mode: str
    backend_ids: tuple[str, ...]
    minimum_agreement: int | None = None


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

    def resolve_formal(
        self,
        formal: dict[str, Any],
        required: Iterable[str],
        implementations: Iterable[str],
    ) -> FormalSelection:
        mode = str(formal.get("mode", ""))
        implementation_set = frozenset(implementations)
        if mode == "pinned":
            backend_ids = (str(formal.get("backend", "")),)
        elif mode == "capability_match":
            eligible = self._eligible("formal", required, implementation_set)
            preference = tuple(str(item) for item in formal.get("backend_preference", []))
            if not preference:
                if len(eligible) != 1:
                    raise CapabilityError(
                        "capability_match requires an explicit backend_preference when matching "
                        f"is not unique; eligible={list(eligible)}"
                    )
                backend_ids = eligible
            else:
                matches = tuple(item for item in preference if item in eligible)
                if not matches:
                    raise CapabilityError(
                        "no preferred formal backend satisfies requirements; "
                        f"eligible={list(eligible)}"
                    )
                backend_ids = (matches[0],)
        elif mode in {"comparison", "agreement_gate"}:
            values = formal.get("backends")
            if not isinstance(values, list) or not values:
                raise CapabilityError(f"{mode} requires a non-empty explicit backends list")
            backend_ids = tuple(str(item) for item in values)
            if len(backend_ids) != len(set(backend_ids)):
                raise CapabilityError(f"{mode} backends must be unique")
        else:
            raise CapabilityError(f"unsupported formal selection mode {mode!r}")
        for backend_id in backend_ids:
            if backend_id not in implementation_set:
                raise CapabilityError(
                    f"strategy package has no formal implementation for {backend_id!r}"
                )
            missing = self.profile("formal", backend_id).missing(required)
            if missing:
                raise CapabilityError(
                    f"formal backend {backend_id!r} lacks required capabilities: {sorted(missing)}"
                )
        minimum = None
        if mode == "agreement_gate":
            minimum = int(formal.get("minimum_agreement", 0))
            if minimum < 2 or minimum > len(backend_ids):
                raise CapabilityError("minimum_agreement must be between 2 and backend count")
        return FormalSelection(mode, backend_ids, minimum)

    def _eligible(
        self,
        role: str,
        required: Iterable[str],
        implementations: frozenset[str],
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in sorted(implementations)
            if (role, name) in self._registrations
            and not self._registrations[(role, name)].profile.missing(required)
        )
