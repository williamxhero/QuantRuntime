from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from conftest import PACKAGE
from strategy_workspace import WorkspaceClient
from test_sandbox_policy import isolated_profile

from quant_runtime.sandbox import SandboxInvocationError, SandboxRunner


class FixtureBackend:
    production = False

    def __init__(self) -> None:
        self.calls = 0
        self.protocol: dict | None = None

    def invoke(self, prepared):
        self.calls += 1
        self.protocol = prepared.protocol
        assert prepared.package.root.is_dir()
        assert (prepared.package.root / "strategy.toml").is_file()
        return {
            "schema": "quant-runtime.sandbox-worker-result.v1",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "success",
            "payload": {"fixture": True},
            "diagnostics": {
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "artifacts": 0,
                "truncated": False,
                "sanitized": True,
            },
        }


def test_safe_fixture_invocation_exposes_only_logical_mounts_and_frozen_identities(
    tmp_path: Path,
) -> None:
    client = WorkspaceClient(tmp_path / "workspace")
    registered = client.register_package(PACKAGE)
    backend = FixtureBackend()
    profile = isolated_profile()
    profile["trust_classification"] = "human_isolated"

    outcome = SandboxRunner(client, backend=backend).invoke(
        package_record=registered,
        profile=profile,
        phase="behavioral_conformance",
        parameters={},
        input_refs={"scenarios": "sha256:" + "d" * 64},
    )

    assert outcome["classification"] == "success"
    assert outcome["payload"] == {"fixture": True}
    assert backend.calls == 1
    assert backend.protocol is not None
    encoded = json.dumps(backend.protocol, sort_keys=True)
    assert str(tmp_path.resolve()) not in encoded
    assert backend.protocol["mounts"] == {
        "package": "/sandbox/package",
        "inputs": "/sandbox/inputs",
        "output": "/sandbox/output",
    }
    assert backend.protocol["package"] == registered["package_ref"]


class ArchiveClient:
    def __init__(self, archive: bytes, package_hash: str) -> None:
        self.archive = archive
        self.package_hash = package_hash

    def verify_artifact(self, uri: str) -> dict:
        return {
            "artifact": {"uri": uri, "sha256": self.package_hash, "bytes": len(self.archive)},
            "verified": True,
        }

    def materialize_artifact(self, uri: str, destination: Path) -> dict:
        del uri
        destination.write_bytes(self.archive)
        return {"path": str(destination.resolve()), "materialized": True}


@pytest.mark.parametrize("member_kind", ["symlink", "fifo"])
def test_unsafe_package_archive_is_rejected_before_backend_call(
    tmp_path: Path, member_kind: str
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("unsafe")
        if member_kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
        else:
            member.type = tarfile.FIFOTYPE
        archive.addfile(member)
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    record = {
        "package_ref": {
            "schema": "quant-research.strategy-package-ref.v1",
            "strategy_id": "unsafe",
            "revision": 1,
            "package_hash": digest,
        },
        "manifest": {"schema": "quant-research.strategy-package.v1"},
        "bundle": {
            "uri": "workspace-artifact://sha256/" + digest,
            "sha256": digest,
            "bytes": len(payload),
        },
    }
    backend = FixtureBackend()
    profile = isolated_profile()
    profile["trust_classification"] = "human_isolated"

    with pytest.raises(SandboxInvocationError, match="special entry"):
        SandboxRunner(ArchiveClient(payload, digest), backend=backend).invoke(
            package_record=record,
            profile=profile,
            phase="behavioral_conformance",
            parameters={},
            input_refs={"scenarios": "sha256:" + "d" * 64},
        )

    assert backend.calls == 0
