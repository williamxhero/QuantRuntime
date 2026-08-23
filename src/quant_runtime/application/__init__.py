"""CLI-neutral application orchestration."""

from .discover import run_discover
from .evaluate import run_evaluate
from .golden import run_golden_check
from .result import ApplicationResult

__all__ = ["ApplicationResult", "run_discover", "run_evaluate", "run_golden_check"]
