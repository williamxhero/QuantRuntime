"""Strategy Workspace public deep interface, loaded lazily to keep adapters acyclic."""

from __future__ import annotations

from typing import Any

__all__ = ["StrategyWorkspace", "resolve_snapshot", "run", "validate_package"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from . import service

    return getattr(service, name)
