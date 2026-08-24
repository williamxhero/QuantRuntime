from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HealthVector:
    data_version: str
    daily_dataset_version: str
    futures_1m_dataset_version: str = ""
