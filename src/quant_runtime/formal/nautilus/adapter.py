from __future__ import annotations

from pathlib import Path
from typing import Any

from .runner import evaluate_candidate


class NautilusFormalRuntime:
    """Execute formal evaluation through NautilusTrader's native engine."""

    name = "nautilus"

    def evaluate(
        self,
        candidate_manifest: Path,
        config: Path,
        output: Path,
    ) -> tuple[dict[str, Any], Path]:
        return evaluate_candidate(candidate_manifest, config, output)
