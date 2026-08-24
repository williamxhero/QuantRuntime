from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any


def normalize_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError(f"non-finite decimal: {value}")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"), parse_float=Decimal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")
    return path


@dataclass(frozen=True, slots=True)
class Artifact:
    relative_path: str
    sha256: str
    content_bytes: int

    @classmethod
    def from_path(cls, path: Path, root: Path) -> Artifact:
        payload = path.read_bytes()
        return cls(
            relative_path=path.relative_to(root).as_posix(),
            sha256=sha256_bytes(payload),
            content_bytes=len(payload),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Artifact:
        artifact = cls(
            relative_path=str(value["relative_path"]),
            sha256=str(value["sha256"]),
            content_bytes=int(value["content_bytes"]),
        )
        if not artifact.relative_path or Path(artifact.relative_path).is_absolute():
            raise ValueError("artifact relative_path must be a non-empty relative path")
        if len(artifact.sha256) != 64:
            raise ValueError("artifact sha256 must contain 64 hexadecimal characters")
        if artifact.content_bytes < 0:
            raise ValueError("artifact content_bytes must be non-negative")
        return artifact

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "content_bytes": self.content_bytes,
        }

    def verify(self, manifest_path: Path) -> Path:
        root = manifest_path.parent.resolve()
        path = (root / self.relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"artifact escapes manifest directory: {self.relative_path}") from exc
        payload = path.read_bytes()
        if len(payload) != self.content_bytes or sha256_bytes(payload) != self.sha256:
            raise ValueError(f"artifact integrity mismatch: {self.relative_path}")
        return path


def artifact_records(root: Path, paths: list[Path]) -> list[dict[str, Any]]:
    return [
        artifact.as_dict()
        for artifact in sorted(
            (Artifact.from_path(path, root) for path in paths),
            key=lambda item: item.relative_path,
        )
    ]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot canonicalize {type(value).__name__}")
