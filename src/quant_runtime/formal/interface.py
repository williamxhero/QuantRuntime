from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class FormalRuntime(Protocol):
    """Neutral application seam implemented by a native formal execution engine."""

    name: str

    def evaluate(
        self,
        candidate_manifest: Path,
        config: Path,
        output: Path,
    ) -> tuple[dict[str, Any], Path]: ...
