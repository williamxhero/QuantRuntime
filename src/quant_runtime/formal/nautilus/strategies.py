"""Legacy import for the first Strategy Package's Nautilus implementation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_strategy_class():
    source = (
        Path(__file__).parents[4]
        / "strategies"
        / "equity"
        / "cross-sectional-momentum"
        / "formal"
        / "nautilus"
        / "strategy.py"
    )
    spec = importlib.util.spec_from_file_location("quant_runtime_momentum_package", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy package implementation: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MomentumTopKStrategy


MomentumTopKStrategy = _load_strategy_class()

__all__ = ["MomentumTopKStrategy"]
