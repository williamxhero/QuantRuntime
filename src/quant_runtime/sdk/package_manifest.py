from __future__ import annotations

import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from quant_runtime.contracts.canonical_hash import sha256_bytes, sha256_value

from .schema import validate_instance

PACKAGE_SCHEMA = "quant-research.strategy-package.v1"


@dataclass(frozen=True, slots=True)
class StrategyPackage:
    root: Path
    manifest: dict[str, Any]
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    package_hash: str

    @property
    def strategy_id(self) -> str:
        return str(self.manifest["strategy_id"])

    @property
    def revision(self) -> int:
        return int(self.manifest["revision"])

    @property
    def requirements(self) -> frozenset[str]:
        return frozenset(str(item) for item in self.manifest["requirements"]["capabilities"])

    @property
    def discovery_policy(self) -> str:
        return str(self.manifest["pipeline"]["discovery"])

    def implementations(self, role: str) -> dict[str, str]:
        value = self.manifest.get("implementations", {}).get(role, {})
        return {str(key): str(item) for key, item in value.items()}

    def resolve_parameters(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        unknown = set(overrides or ()) - set(self.default_parameters)
        if unknown:
            raise ValueError(f"unknown strategy parameters: {sorted(unknown)}")
        resolved = {**self.default_parameters, **(overrides or {})}
        _validate_parameters(self.parameter_schema, resolved)
        return resolved

    def parameters_hash(self, overrides: dict[str, Any] | None = None) -> str:
        return sha256_value(self.resolve_parameters(overrides))

    def resolve_entrypoint(self, role: str, backend_id: str) -> str:
        try:
            return self.implementations(role)[backend_id]
        except KeyError as exc:
            raise ValueError(
                f"strategy package has no {role} implementation for {backend_id!r}"
            ) from exc


def validate_package(path: Path) -> StrategyPackage:
    root = path.resolve()
    manifest_path = root / "strategy.toml" if root.is_dir() else root
    root = manifest_path.parent
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read strategy package {manifest_path}: {exc}") from exc
    validate_instance("strategy-package.v1", manifest)
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise ValueError(f"unsupported strategy package schema {manifest.get('schema')!r}")
    schema_path = _contained_file(root, str(manifest["parameter_schema"]))
    try:
        parameter_schema = __import__("json").loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read parameter schema {schema_path}: {exc}") from exc
    if not isinstance(parameter_schema, dict):
        raise ValueError("parameter schema root must be an object")
    Draft202012Validator.check_schema(parameter_schema)
    defaults = _full_defaults(parameter_schema)
    _validate_parameters(parameter_schema, defaults)
    for role in ("discovery", "formal"):
        for entrypoint in manifest.get("implementations", {}).get(role, {}).values():
            relative = str(entrypoint).partition(":")[0]
            _contained_file(root, relative)
    return StrategyPackage(
        root=root,
        manifest=manifest,
        parameter_schema=parameter_schema,
        default_parameters=defaults,
        package_hash=_package_hash(root),
    )


def _full_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("parameter schema must be a closed object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("parameter schema properties must be an object")
    required = schema.get("required")
    if set(required or ()) != set(properties):
        raise ValueError("every parameter must be required; defaults provide the complete baseline")
    missing = sorted(name for name, item in properties.items() if "default" not in item)
    if missing:
        raise ValueError(f"every parameter requires a default: {missing}")
    return {name: item["default"] for name, item in properties.items()}


def _validate_parameters(schema: dict[str, Any], parameters: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(parameters),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = "/".join(str(item) for item in error.absolute_path) or "<root>"
        raise ValueError(f"strategy parameters invalid at {location}: {error.message}")


def _contained_file(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError(f"package file must be a non-empty relative path: {relative!r}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"package file escapes package root: {relative!r}") from exc
    if not path.is_file():
        raise ValueError(f"package file does not exist: {relative!r}")
    return path


def _package_hash(root: Path) -> str:
    digest = sha256()
    files = sorted(
        path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts
    )
    if not files:
        raise ValueError("strategy package is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(bytes.fromhex(sha256_bytes(payload)))
    return digest.hexdigest()
