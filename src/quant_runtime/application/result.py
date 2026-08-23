from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    payload: dict[str, Any]
    exit_code: int
