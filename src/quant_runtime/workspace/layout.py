from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    root: Path

    @classmethod
    def create(cls, root: Path) -> RuntimeLayout:
        layout = cls(root.resolve())
        for path in (
            layout.snapshots,
            layout.runs,
            layout.evidence,
            layout.cache,
            layout.staging,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return layout

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def staging(self) -> Path:
        return self.root / "staging"
