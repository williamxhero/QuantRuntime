"""CLI-neutral application orchestration."""

from .discover import run_discover
from .evaluate import run_evaluate
from .golden import run_golden_check
from .result import ApplicationResult
from .workspace import run_package_validate, run_snapshot_resolve, run_workspace

__all__ = [
    "ApplicationResult",
    "run_discover",
    "run_evaluate",
    "run_golden_check",
    "run_package_validate",
    "run_snapshot_resolve",
    "run_workspace",
]
