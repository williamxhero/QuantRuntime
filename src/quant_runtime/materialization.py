from __future__ import annotations

import hashlib
import tarfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from quant_runtime.package import StrategyPackage


class PackageMaterializationError(ValueError):
    """Verified package bytes cannot be materialized without weakening identity."""


class WorkspacePackageArtifactPort(Protocol):
    def verify_artifact(self, artifact_uri: str) -> dict[str, Any]: ...
    def materialize_artifact(self, artifact_uri: str, destination: Path) -> dict[str, Any]: ...


class VerifiedPackageMaterializer:
    """The sole Runtime contract for fetching and extracting registered package bytes."""

    def __init__(self, client: WorkspacePackageArtifactPort) -> None:
        self._client = client

    def materialize(self, record: Mapping[str, Any], destination: Path) -> StrategyPackage:
        package_record = {str(key): value for key, value in record.items()}
        package_ref = _object(package_record, "package_ref")
        bundle = _object(package_record, "bundle")
        uri = str(bundle.get("uri", ""))
        expected = str(bundle.get("sha256", ""))
        if not uri or len(expected) != 64 or package_ref.get("package_hash") != expected:
            raise PackageMaterializationError("registered package bundle identity is invalid")
        verified = self._client.verify_artifact(uri)
        artifact = _object(verified, "artifact")
        if verified.get("verified") is not True or artifact.get("sha256") != expected:
            raise PackageMaterializationError("registered package bundle verification failed")
        destination = Path(destination)
        if destination.exists():
            raise PackageMaterializationError("isolated package destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = destination.parent / "package.tar"
        materialized = self._client.materialize_artifact(uri, archive)
        if Path(str(materialized.get("path", ""))).resolve() != archive.resolve():
            raise PackageMaterializationError("package materialization destination mismatch")
        payload = archive.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise PackageMaterializationError("registered package bundle bytes drifted")
        destination.mkdir(parents=False, exist_ok=False)
        _extract_verified_tar(archive, destination)
        _verify_declared_content(package_record, destination)
        return StrategyPackage.from_record(package_record, root=destination)


def _extract_verified_tar(archive_path: Path, destination: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if (
                not member.name
                or "\\" in member.name
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != member.name
            ):
                raise PackageMaterializationError("package archive contains an unsafe path")
            key = "/".join(
                unicodedata.normalize("NFKC", part).casefold() for part in relative.parts
            )
            if key in seen:
                raise PackageMaterializationError("package archive contains an aliased path")
            seen.add(key)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise PackageMaterializationError("package archive contains a special entry")
            source = archive.extractfile(member)
            if source is None:
                raise PackageMaterializationError("package archive file cannot be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def _verify_declared_content(record: Mapping[str, Any], root: Path) -> None:
    manifest = _object(record, "manifest")
    if manifest.get("schema") != "quant-research.strategy-package.v2":
        return
    raw = record.get("declared_content")
    if not isinstance(raw, list):
        raise PackageMaterializationError("generated package lacks declared content")
    expected = {
        str(item["path"]): str(item["sha256"])
        for item in raw
        if isinstance(item, Mapping) and set(item) >= {"path", "sha256"}
    }
    if len(expected) != len(raw):
        raise PackageMaterializationError("generated package declared content is invalid")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(expected) | {"strategy.toml"}:
        raise PackageMaterializationError("generated package content inventory drifted")
    for relative, digest in expected.items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise PackageMaterializationError("generated package content digest drifted")


def _object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise PackageMaterializationError(f"registered package lacks {name}")
    return {str(key): member for key, member in item.items()}
