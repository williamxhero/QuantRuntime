from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4


class AtomicDirectory:
    def __init__(self, staging_root: Path, final: Path | None = None) -> None:
        self.final = final
        name = final.name if final is not None else "snapshot"
        self.path = staging_root / f"{name}-{uuid4().hex}"
        self._published = False

    def __enter__(self) -> AtomicDirectory:
        self.path.mkdir(parents=True, exist_ok=False)
        return self

    def publish(self, final: Path | None = None) -> Path:
        target = final or self.final
        if target is None:
            raise ValueError("atomic directory target is not set")
        if target.exists():
            raise FileExistsError(f"immutable target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.path, target)
        self._published = True
        self.final = target
        return target

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._published and self.path.exists():
            shutil.rmtree(self.path)
