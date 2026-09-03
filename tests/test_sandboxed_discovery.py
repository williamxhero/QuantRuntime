from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PACKAGE, FixtureTransport
from strategy_workspace import WorkspaceClient, WorkspaceWorker
from test_behavioral_conformance import ConformanceBackend
from test_executor_topologies import request as legacy_request
from test_preflight import draft as preflight_draft
from test_preflight import preflight
from test_sandbox_policy import isolated_profile

from quant_runtime.adapters.data.markethub import (
    MarketHubClient,
    MarketHubDataAdapter,
    ResolvedSnapshot,
)
from quant_runtime.adapters.discovery.qlib.capsule import (
    build_discovery_capsule,
    capsule_bytes,
)
from quant_runtime.adapters.interface import FormalAdapterResult
from quant_runtime.artifacts import canonical_json
from quant_runtime.capabilities import AdapterRegistry, CapabilityProfile
from quant_runtime.conformance import RuntimeConformance
from quant_runtime.executor import RuntimeExecutor
from quant_runtime.sandbox import SandboxRunner
from quant_runtime.sandbox.oci import (
    BACKEND_ID,
    MECHANISM,
    MECHANISM_VERSION,
    OciSandboxBackend,
    OciSandboxConfig,
)
from quant_runtime.sandbox.outcome import bounded_diagnostics, sandbox_outcome

IMAGE = "sha256:786db3c1998c8355621e4941f1235735356695557b6dbb8f49213958e19f6370"


def generated_package(root: Path, *, marker: Path) -> Path:
    root.mkdir()
    files = {
        "parameters.schema.json": (
            "parameter-schema",
            (PACKAGE / "parameters.schema.json").read_bytes(),
        ),
        "discovery.py": (
            "discovery-entrypoint",
            (
                "from pathlib import Path\n"
                "import os\n"
                "if os.environ.get('QUANT_RUNTIME_PARENT_MARKER'):\n"
                f"    Path({str(marker)!r}).write_text('parent-imported')\n"
                "def discover(frame, parameters):\n"
                "    del parameters\n"
                "    signals=frame[['close']].rename(columns={'close':'score'})\n"
                "    signals['label']=0.0\n"
                "    return {'signals':signals,'rank_ic':signals['label'],"
                "'candidates':signals.iloc[:1],'risk':signals[['label']]}\n"
            ).encode(),
        ),
        "strategy.py": ("formal-entrypoint", (PACKAGE / "strategy.py").read_bytes()),
        "provenance.json": (
            "provenance",
            json.dumps(
                {
                    "schema": "apex-research.package-provenance-binding.v1",
                    "records": [
                        {
                            "record_type": "apex-research.strategy-candidate.v1",
                            "record_id": "candidate.generated",
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
    }
    for relative, (_, content) in files.items():
        (root / relative).write_bytes(content)
    contents = "\n\n".join(
        "[[contents]]\n"
        f'path = "{relative}"\n'
        f'role = "{role}"\n'
        f'sha256 = "{hashlib.sha256(content).hexdigest()}"'
        for relative, (role, content) in sorted(files.items())
    )
    (root / "strategy.toml").write_text(
        f'''schema = "quant-research.strategy-package.v2"
strategy_id = "generated.discovery"
revision = 1
display_name = "Generated discovery"
parameter_schema = "parameters.schema.json"

[pipeline]
discovery = "optional"
formal = "required"

[requirements]
asset_classes = ["equity"]
frequencies = ["1d"]
capabilities = ["data.bar.1d"]
decision_intents = ["target_weight"]

[[requirements.data]]
capability = "data.bar.1d"
frequency = "1d"
adjustment = "none"
semantics = [{{ dimension = "time", required = true }}]

[implementations.discovery]
qlib = "discovery.py:discover"

[implementations.formal]
nautilus = "strategy.py:ObservedBarFixtureStrategy"

[dependencies]
qlib_api = "0.9.7"

[provenance]
binding_path = "provenance.json"
binding_sha256 = "{hashlib.sha256(files["provenance.json"][1]).hexdigest()}"

[[provenance.records]]
record_type = "apex-research.strategy-candidate.v1"
record_id = "candidate.generated"

{contents}
''',
        encoding="utf-8",
        newline="\n",
    )
    return root


class SandboxedDiscoveryBackend:
    production = False

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.calls = 0

    def invoke(self, prepared):
        self.calls += 1
        assert not self.marker.exists()
        assert prepared.protocol["phase"] == "discovery"
        assert prepared.protocol["phase_config"]["adapter"] == "qlib"
        assert set(prepared.protocol["input_refs"]) == {
            "discovery-capsule.json",
            "parameters.json",
        }
        output = prepared.output / "staging"
        output.mkdir()
        (output / "qlib_signals.csv").write_text("score\n1\n", encoding="utf-8")
        diagnostics = bounded_diagnostics(
            limits=prepared.protocol["sandbox_profile"]["limits"],
            stdout_bytes=0,
            stderr_bytes=0,
            artifact_count=1,
            artifact_bytes=8,
            artifacts_accepted=1,
            terminal_proof={
                "backend_id": "fixture",
                "mechanism_version": "fixture",
                "proof_id": "sha256:" + "f" * 64,
                "candidate_terminated": True,
                "descendants_terminated": True,
                "running_processes": 0,
            },
        )
        payload = {
            "backend_id": "qlib",
            "adapter_version": "test",
            "engine_version": "test",
            "artifact_hash": "d" * 64,
            "metrics": {"candidate_rows": 1},
            "evidence": [],
        }
        outcome = sandbox_outcome("success", diagnostics=diagnostics, payload=payload)
        return {
            "schema": "quant-runtime.sandbox-worker-result.v2",
            "invocation_id": prepared.protocol["invocation_id"],
            "classification": "success",
            "payload": payload,
            "sandbox": outcome,
        }


class ParentDiscoveryMustNotRun:
    name = "qlib"

    def __init__(self) -> None:
        raise AssertionError("parent Runtime constructed the generated discovery adapter")


class BoundaryFormalAdapter:
    name = "nautilus"

    def run(self, value, *, formal_id: str):
        assert not hasattr(value, "discovery")
        return FormalAdapterResult(
            formal_id=formal_id,
            backend_id="nautilus",
            adapter_version="test",
            engine_version="test",
            status="completed",
            metrics={"score": 1.0},
            positions=(),
            fills=(),
            account_curve=(),
            native_evidence=(),
        )


def _registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(
        CapabilityProfile.from_dict(
            {
                "backend_id": "qlib",
                "role": "discovery",
                "adapter_version": "test",
                "engine_version": "test",
                "provides": ["data.bar.1d"],
            }
        ),
        ParentDiscoveryMustNotRun,
    )
    registry.register(
        CapabilityProfile.from_dict(
            {
                "backend_id": "nautilus",
                "role": "formal",
                "adapter_version": "test",
                "engine_version": "test",
                "provides": ["data.bar.1d"],
            }
        ),
        BoundaryFormalAdapter,
    )
    return registry


def test_generated_discovery_runs_through_sandbox_without_crossing_formal_boundary(
    tmp_path: Path, market_fixture: dict
) -> None:
    marker = tmp_path / "parent-imported"
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(generated_package(tmp_path / "generated", marker=marker))
    scenario = client.publish_record(
        {
            "record_id": "scenario.discovery",
            "record_type": "quant-runtime.behavioral-scenarios.v1",
            "payload": {},
        },
        artifacts=(
            {
                "source": b"{}",
                "media_type": "application/json",
                "logical_role": "behavioral-scenario",
                "name": "scenario.json",
            },
        ),
    )
    profile = isolated_profile()
    profile["limits"]["processes"] = 1
    conformance = RuntimeConformance(client, backend=ConformanceBackend()).conform(
        {
            "schema": "quant-research.runtime-conformance-request.v1",
            "strategy_package": package["package_ref"],
            "parameters": {},
            "sandbox_profile": profile,
            "behavioral_scenarios": scenario["artifacts"],
        }
    )["behavioral_conformance"]
    draft = preflight_draft(package["package_ref"])
    draft.update(
        {
            "schema": "quant-research.runtime-preflight-request.v2",
            "sandbox_profile": profile,
            "behavioral_conformance": conformance,
            "execution": {
                "topology": "discovery_formal",
                "discovery": {"adapter": "qlib", "config": {}},
                "formal": [{"id": "primary", "adapter": "nautilus", "config": {}}],
            },
        }
    )
    prepared = preflight(workspace, market_fixture).preflight(draft)
    submitted = client.submit_run(
        {
            "schema": "quant-research.workspace-run-request.v4",
            "strategy_package": package["package_ref"],
            "market_snapshot": prepared["frozen_snapshot"],
            "parameters": {},
            "sandbox_profile": profile,
            "behavioral_conformance": conformance,
            "execution": draft["execution"],
        }
    )
    backend = SandboxedDiscoveryBackend(marker)
    completed = RuntimeExecutor(
        client,
        WorkspaceWorker(workspace),
        registry=_registry(),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(market_fixture))
        ),
        sandbox_backend=backend,
    ).execute(submitted["run_id"])

    assert completed["status"] == "completed"
    assert completed["result"]["schema"] == "quant-research.result.v3"
    assert completed["result"]["sandbox"]["classification"] == "success"
    assert completed["result"]["discovery"]["adapter"] == "qlib"
    assert any(
        item["name"].endswith("qlib_signals.csv") for item in completed["result"]["artifacts"]
    )
    assert backend.calls == 1
    assert not marker.exists()


def test_formal_only_topology_makes_zero_qlib_calls(tmp_path: Path, market_fixture: dict) -> None:
    workspace = tmp_path / "workspace"
    client = WorkspaceClient(workspace)
    package = client.register_package(PACKAGE)
    submitted = client.submit_run(legacy_request(package["package_ref"], "formal_only"))

    completed = RuntimeExecutor(
        client,
        WorkspaceWorker(workspace),
        registry=_registry(),
        data_adapter=MarketHubDataAdapter(
            client_factory=lambda _: MarketHubClient(transport=FixtureTransport(market_fixture))
        ),
    ).execute(submitted["run_id"])

    assert completed["status"] == "completed"
    assert "discovery" not in completed["result"]


def _image_ready() -> bool:
    executable = shutil.which("docker")
    if executable is None:
        return False
    return (
        subprocess.run(
            [executable, "image", "inspect", IMAGE],
            check=False,
            capture_output=True,
            timeout=15,
            shell=False,
        ).returncode
        == 0
    )


@pytest.mark.oci
@pytest.mark.skipif(not _image_ready(), reason="exact production worker image is unavailable")
def test_generated_qlib_entrypoint_imports_only_inside_exact_oci_worker(
    tmp_path: Path, canonical_dataset, monkeypatch
) -> None:
    marker = tmp_path / "parent-imported"
    monkeypatch.setenv("QUANT_RUNTIME_PARENT_MARKER", "1")
    client = WorkspaceClient(tmp_path / "workspace")
    package = client.register_package(generated_package(tmp_path / "generated", marker=marker))
    snapshot = ResolvedSnapshot(
        {
            "snapshot_id": "sha256:" + "a" * 64,
            "resolved_at": "2026-09-03T00:00:00Z",
        },
        tmp_path / "snapshot.json",
        canonical_dataset,
    )
    capsule = build_discovery_capsule(snapshot)
    publication = client.publish_record(
        {
            "record_id": "sandbox-input.oci-discovery",
            "record_type": "quant-runtime.sandbox-input.v1",
            "payload": {},
        },
        artifacts=(
            {
                "source": capsule_bytes(capsule),
                "logical_role": "sandbox-input",
                "name": "discovery-capsule.json",
            },
            {
                "source": canonical_json({}),
                "logical_role": "sandbox-input",
                "name": "parameters.json",
            },
        ),
    )
    backend = OciSandboxBackend(OciSandboxConfig(image=IMAGE))
    proof = backend.capability_proof(refresh=True)
    profile = isolated_profile()
    profile["containment"] = {
        "backend_id": BACKEND_ID,
        "mechanism": MECHANISM,
        "mechanism_version": MECHANISM_VERSION,
        "platform": "linux",
        "proof": proof["proof_id"],
    }
    profile["dependency_environment"] = {
        "kind": "oci-image",
        "identity": IMAGE,
        "lock_identity": proof["dependency_lock_identity"],
    }
    profile["limits"].update(
        {
            "memory_bytes": 536_870_912,
            "wall_clock_seconds": 30,
            "processes": 1,
            "filesystem_bytes": 10_485_760,
        }
    )
    output = tmp_path / "output"

    result = SandboxRunner(client, backend=backend).invoke(
        package_record=package,
        profile=profile,
        phase="discovery",
        parameters={},
        input_artifacts={item["name"]: item for item in publication["artifacts"]},
        phase_config={
            "adapter": "qlib",
            "config": {},
            "entrypoint": "discovery.py:discover",
            "snapshot_id": snapshot.snapshot_id,
        },
        output_destination=output,
    )

    assert result["classification"] == "success", result
    assert result["payload"]["backend_id"] == "qlib"
    assert result["payload"]["engine_version"] == "0.9.7"
    assert {path.name for path in output.iterdir()} == {
        "discovery_manifest.json",
        "qlib_candidates.csv",
        "qlib_rank_ic.csv",
        "qlib_risk.csv",
        "qlib_signals.csv",
    }
    assert result["sandbox"]["diagnostics"]["terminal_proof"]["running_processes"] == 0
    assert not marker.exists()
