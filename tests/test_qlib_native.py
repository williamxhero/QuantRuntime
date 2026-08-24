from __future__ import annotations

from pathlib import Path

from conftest import PACKAGE
from strategy_workspace import WorkspaceClient
from test_executor_topologies import snapshot

from quant_runtime.adapters.data.markethub import ResolvedSnapshot
from quant_runtime.adapters.discovery.qlib import QlibDiscoveryAdapter
from quant_runtime.adapters.interface import DiscoveryRunInput
from quant_runtime.package import StrategyPackage


def test_qlib_preserves_native_research_evidence(tmp_path: Path, canonical_dataset) -> None:
    registered = WorkspaceClient(tmp_path / "workspace").register_package(PACKAGE)
    package = StrategyPackage.from_record(registered, root=PACKAGE)
    output = tmp_path / "qlib"

    result = QlibDiscoveryAdapter().run(
        DiscoveryRunInput(
            package=package,
            parameters={},
            snapshot=ResolvedSnapshot(snapshot(), tmp_path / "snapshot.json", canonical_dataset),
            output=output,
            config={},
        )
    )

    assert result.backend_id == "qlib"
    assert result.engine_version
    assert result.artifact_hash
    assert {item["relative_path"] for item in result.evidence} == {
        "discovery_manifest.json",
        "qlib_candidates.csv",
        "qlib_rank_ic.csv",
        "qlib_risk.csv",
        "qlib_signals.csv",
    }
