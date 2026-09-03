from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant_runtime.adapters.data.markethub import ResolvedSnapshot
from quant_runtime.adapters.data.markethub.model import CanonicalDataset
from quant_runtime.artifacts import canonical_json, sha256_value

CAPSULE_SCHEMA = "quant-runtime.qlib-discovery-capsule.v1"
NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "amount", "pre_close")


def build_discovery_capsule(snapshot: ResolvedSnapshot) -> dict[str, Any]:
    dataset = snapshot.dataset
    if not isinstance(dataset, CanonicalDataset):
        raise ValueError("Qlib discovery requires a verified canonical daily snapshot")
    identity = {
        "schema": CAPSULE_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "canonical_input_hash": dataset.input_hash,
        "records": [bar.hash_record() for bar in dataset.bars],
    }
    return {**identity, "capsule_id": "sha256:" + sha256_value(identity)}


def capsule_bytes(capsule: dict[str, Any]) -> bytes:
    return canonical_json(capsule)


def load_discovery_capsule(path: Path, *, snapshot_id: str) -> pd.DataFrame:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Qlib discovery capsule cannot be read") from exc
    if not isinstance(value, dict) or value.get("schema") != CAPSULE_SCHEMA:
        raise ValueError("Qlib discovery capsule schema is invalid")
    supplied_id = value.pop("capsule_id", None)
    if supplied_id != "sha256:" + sha256_value(value):
        raise ValueError("Qlib discovery capsule identity is invalid")
    if value.get("snapshot_id") != snapshot_id:
        raise ValueError("Qlib discovery capsule snapshot identity differs")
    records = value.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Qlib discovery capsule has no records")
    frame = pd.DataFrame.from_records(records)
    required = {"trading_day", "instrument", *NUMERIC_COLUMNS, "is_suspended", "is_st"}
    if set(frame.columns) != required:
        raise ValueError("Qlib discovery capsule record shape is invalid")
    frame = frame.rename(columns={"trading_day": "datetime"})
    frame["datetime"] = pd.to_datetime(frame["datetime"], format="%Y-%m-%d")
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    frame = frame.set_index(["datetime", "instrument"]).sort_index()
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Qlib discovery capsule records are not canonical")
    return frame
