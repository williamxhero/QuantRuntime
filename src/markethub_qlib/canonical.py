from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import pandas as pd


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def hash_frame(frame: pd.DataFrame, *, data_version: str, dataset_version: str) -> str:
    ordered = frame.sort_index()
    records: list[dict[str, Any]] = []
    for (timestamp, instrument), row in ordered.iterrows():
        record: dict[str, Any] = {
            "datetime": pd.Timestamp(timestamp).date().isoformat(),
            "instrument": str(instrument),
        }
        for column in ordered.columns:
            record[column] = _normalize_scalar(row[column])
        records.append(record)
    return sha256_value(
        {
            "schema": "markethub-qlib.canonical-input.v1",
            "data_version": data_version,
            "dataset_version": dataset_version,
            "records": records,
        }
    )


def _normalize_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            raise ValueError(f"non-finite numeric value: {value!r}")
        rendered = format(decimal, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _normalize_scalar(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    raise TypeError(f"cannot canonicalize {type(value).__name__}")
