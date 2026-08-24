from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AdapterStorage:
    root: Path

    @classmethod
    def create(cls, root: Path) -> AdapterStorage:
        layout = cls(root.resolve())
        for path in (
            layout.snapshots,
            layout.cache,
            layout.staging,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return layout

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def staging(self) -> Path:
        return self.root / "staging"
