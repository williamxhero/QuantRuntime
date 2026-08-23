from __future__ import annotations

from collections.abc import Callable

from .interface import FormalRuntime

RuntimeFactory = Callable[[], FormalRuntime]


def _nautilus_runtime() -> FormalRuntime:
    from .nautilus.adapter import NautilusFormalRuntime

    return NautilusFormalRuntime()


_RUNTIMES: dict[str, RuntimeFactory] = {"nautilus": _nautilus_runtime}


def formal_runtime_names() -> tuple[str, ...]:
    return tuple(sorted(_RUNTIMES))


def get_formal_runtime(name: str) -> FormalRuntime:
    try:
        factory = _RUNTIMES[name]
    except KeyError as exc:
        supported = ", ".join(formal_runtime_names())
        raise ValueError(
            f"unsupported formal runtime {name!r}; choose one of: {supported}"
        ) from exc
    return factory()
