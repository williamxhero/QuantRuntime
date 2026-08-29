from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_package_entrypoint(package_root: Path, entrypoint: str) -> Any:
    relative, separator, attribute = entrypoint.partition(":")
    if not separator or not attribute:
        raise ValueError(f"entrypoint must use relative/path.py:attribute syntax: {entrypoint!r}")
    resolved_package_root = package_root.resolve()
    source = (resolved_package_root / relative).resolve()
    try:
        source.relative_to(resolved_package_root)
    except ValueError as exc:
        raise ValueError(f"entrypoint escapes package root: {entrypoint!r}") from exc
    if not source.is_file():
        raise ValueError(f"entrypoint source does not exist: {entrypoint!r}")
    module_name = f"quant_runtime_package_{source.stem}_{abs(hash(source))}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package entrypoint: {entrypoint!r}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise ImportError(f"package entrypoint attribute is missing: {entrypoint!r}") from exc
