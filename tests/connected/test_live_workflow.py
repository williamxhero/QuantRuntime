from __future__ import annotations

from pathlib import Path

import pytest

from quant_runtime.application import run_evaluate
from quant_runtime.contracts.formal_manifest import FormalManifest
from quant_runtime.discovery.qlib.candidate_manifest import write_candidate_run
from quant_runtime.discovery.qlib.workflow import DiscoveryConfig, run_discovery
from quant_runtime.formal.nautilus.runner import FormalConfig, run_engine

ROOT = Path(__file__).parents[2]


@pytest.mark.connected
def test_live_discover_evaluate_contract_matches(tmp_path: Path) -> None:
    discovery_config = DiscoveryConfig.load(
        ROOT / "configs" / "discovery" / "qlib" / "s-momentum.json"
    )
    result = run_discovery(discovery_config)
    candidate, candidate_path = write_candidate_run(
        discovery_config, result, tmp_path / "candidate"
    )
    evaluation = run_evaluate(
        candidate_path,
        ROOT / "configs" / "formal" / "nautilus" / "s-momentum.json",
        tmp_path / "formal",
    )
    formal = FormalManifest.load(Path(evaluation.payload["manifest_path"]))
    assert candidate["metrics"]["reference_decision_hash"] == formal.formal_decision_hash
    assert evaluation.exit_code == 0
    assert formal.semantic_match is True
    assert formal.status == "matched"


@pytest.mark.connected
@pytest.mark.engine
def test_live_formal_runtime_is_deterministic(tmp_path: Path) -> None:
    discovery_config = DiscoveryConfig.load(
        ROOT / "configs" / "discovery" / "qlib" / "s-momentum.json"
    )
    discovery = run_discovery(discovery_config)
    formal_config = FormalConfig.load(ROOT / "configs" / "formal" / "nautilus" / "s-momentum.json")
    outputs = [
        run_engine(discovery.dataset, formal_config, tmp_path / f"repeat-{index}")
        for index in range(3)
    ]
    assert len({item.decision_hash for item in outputs}) == 1
    assert len({item.output_hash for item in outputs}) == 1
    assert outputs[0].decision_hash
    assert all(len(item.fills) == 5 for item in outputs)
