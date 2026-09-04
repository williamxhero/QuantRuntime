from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quant_runtime import cli
from quant_runtime.artifacts import sha256_value
from quant_runtime.transport import (
    MAX_TRANSPORT_BYTES,
    TransportContractError,
    read_transport_json,
    validate_binding,
)


def _binding(request: dict, preflight: dict) -> dict:
    identity = {
        "schema": "quant-runtime.validation-binding.v1",
        "protocol_id": "1" * 64,
        "cell_id": "2" * 64,
        "request_sha256": sha256_value(request),
        "preflight_sha256": sha256_value(preflight),
        "runtime_capability_id": cli.runtime_capabilities()["capability_id"],
    }
    return {**identity, "binding_id": sha256_value(identity)}


@pytest.mark.parametrize(
    "content",
    [
        b'[{"value":1}]',
        b'{"value":1,"value":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"value":',
    ],
)
def test_transport_rejects_noncanonical_or_malformed_input(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(content)

    with pytest.raises(TransportContractError):
        read_transport_json(path)


def test_transport_rejects_oversize_link_special_and_missing_without_path_leak(
    tmp_path: Path,
) -> None:
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (MAX_TRANSPORT_BYTES + 1))
    with pytest.raises(TransportContractError, match="size limit"):
        read_transport_json(oversize)

    missing = tmp_path / "secret-name.json"
    with pytest.raises(TransportContractError) as missing_error:
        read_transport_json(missing)
    assert str(missing) not in str(missing_error.value)

    if hasattr(os, "symlink"):
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / "link.json"
        try:
            link.symlink_to(target)
        except OSError:
            pass
        else:
            with pytest.raises(TransportContractError, match="non-link"):
                read_transport_json(link)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "unknown"),
        ("protocol_id", "A" * 64),
        ("cell_id", "1" * 63),
        ("request_sha256", "0" * 64),
        ("preflight_sha256", "0" * 64),
        ("runtime_capability_id", "0" * 64),
        ("binding_id", "0" * 64),
    ],
)
def test_binding_fails_closed_on_every_identity_mismatch(field: str, value: str) -> None:
    request = {"schema": "request"}
    preflight = {"schema": "preflight"}
    binding = _binding(request, preflight)
    binding[field] = value

    with pytest.raises(TransportContractError):
        validate_binding(
            binding,
            request,
            preflight,
            str(cli.runtime_capabilities()["capability_id"]),
        )


def test_frozen_pair_is_required_before_workspace_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Forbidden:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("Workspace access must remain zero")

    monkeypatch.setattr(cli, "WorkspaceClient", Forbidden)
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")

    code = cli.main(
        [
            "run",
            "--request",
            str(request),
            "--frozen-preflight",
            str(request),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert str(request) not in payload["error"]["message"]


def test_unknown_frozen_request_fails_before_workspace_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Forbidden:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("Workspace access must remain zero")

    monkeypatch.setattr(cli, "WorkspaceClient", Forbidden)
    request = {"schema": "unknown"}
    preflight = {"schema": "unknown"}
    binding = _binding(request, preflight)
    paths = []
    for name, value in (("request", request), ("preflight", preflight), ("binding", binding)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)

    code = cli.main(
        [
            "run",
            "--request",
            str(paths[0]),
            "--frozen-preflight",
            str(paths[1]),
            "--validation-binding",
            str(paths[2]),
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_frozen_transport_is_opened_once_and_execution_uses_in_memory_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = {
        "schema": "quant-research.runtime-preflight-request.v3",
        "strategy_package": {"package_hash": "1" * 64},
        "snapshot_request": {},
        "parameters": {},
        "execution": {},
        "sandbox_profile": {},
        "behavioral_conformance": {},
    }
    preflight = {
        "schema": "quant-research.runtime-preflight-result.v1",
        "status": "accepted",
        "frozen_snapshot": {"snapshot_id": "sha256:" + "3" * 64},
        "evidence": {},
    }
    binding = _binding(request, preflight)
    paths: list[Path] = []
    for name, value in (("request", request), ("preflight", preflight), ("binding", binding)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)
    reads: list[Path] = []

    def mutating_reader(path: Path) -> dict:
        value = read_transport_json(path)
        reads.append(path)
        path.write_text('{"tampered":true}', encoding="utf-8")
        return value

    class Client:
        def __init__(self, root: Path):
            del root

        def submit_run(self, canonical: dict) -> dict:
            assert canonical["market_snapshot"] == preflight["frozen_snapshot"]
            return {"run_id": "run-1"}

    class Worker:
        def __init__(self, root: Path):
            del root

    class Executor:
        def __init__(self, client: Client, worker: Worker):
            del client, worker

        def execute(self, run_id: str) -> dict:
            return {"run_id": run_id, "status": "completed"}

    monkeypatch.setattr(cli, "read_transport_json", mutating_reader)
    monkeypatch.setattr(cli, "validate_frozen_transport", lambda draft, result: None)
    monkeypatch.setattr(
        cli, "validate_frozen_preflight", lambda client, draft, result: None
    )
    monkeypatch.setattr(cli, "WorkspaceClient", Client)
    monkeypatch.setattr(cli, "WorkspaceWorker", Worker)
    monkeypatch.setattr(cli, "RuntimeExecutor", Executor)

    code = cli.main(
        [
            "run",
            "--request",
            str(paths[0]),
            "--frozen-preflight",
            str(paths[1]),
            "--validation-binding",
            str(paths[2]),
        ]
    )

    assert code == 0
    assert reads == paths
    assert json.loads(capsys.readouterr().out)["request_id"] == "run-1"
