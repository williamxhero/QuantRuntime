from __future__ import annotations

from inspect import signature
from pathlib import Path

from conftest import FixtureTransport

from quant_runtime.discovery.qlib.candidate_manifest import write_candidate_run
from quant_runtime.discovery.qlib.workflow import DiscoveryConfig, run_discovery
from quant_runtime.formal.nautilus import runner as formal_runner
from quant_runtime.formal.nautilus.runner import FormalConfig, run_engine
from quant_runtime.formal.nautilus.strategies import MomentumTopKStrategy
from quant_runtime.market_data.markethub.client import MarketHubClient
from quant_runtime.semantics.golden_compare import compare_manifests

ROOT = Path(__file__).parents[2]


def test_fixture_discover_evaluate_and_golden_check_match(
    fixture_client: MarketHubClient,
    monkeypatch,
    s_fixture,
    tmp_path: Path,
) -> None:
    discovery_config = DiscoveryConfig.load(
        ROOT / "configs" / "discovery" / "qlib" / "s-momentum.json"
    )
    discovery = run_discovery(discovery_config, client_factory=lambda _: fixture_client)
    candidate, candidate_path = write_candidate_run(
        discovery_config, discovery, tmp_path / "candidate"
    )
    monkeypatch.setattr(
        formal_runner,
        "MarketHubClient",
        lambda *args, **kwargs: MarketHubClient(transport=FixtureTransport(s_fixture)),
    )
    formal, formal_path = formal_runner.evaluate_candidate(
        candidate_path,
        ROOT / "configs" / "formal" / "nautilus" / "s-momentum.json",
        tmp_path / "formal",
    )
    report = compare_manifests(candidate_path, formal_path)
    assert candidate["schema"] == "quant-runtime.candidate-manifest.v1"
    assert formal["schema"] == "quant-runtime.formal-manifest.v1"
    assert formal["metrics"]["semantic_match"] is True
    assert formal["metrics"]["candidate_decision_hash"] == formal["metrics"]["formal_decision_hash"]
    assert report["status"] == "matched"
    assert len(formal["artifacts"]) == 7


def test_formal_engine_has_no_candidate_or_reference_injection(
    canonical_dataset,
    tmp_path: Path,
) -> None:
    parameters = signature(MomentumTopKStrategy).parameters
    assert "candidate" not in parameters
    assert "reference" not in parameters
    assert "dataset" not in parameters
    config = FormalConfig.load(ROOT / "configs" / "formal" / "nautilus" / "s-momentum.json")
    results = [
        run_engine(canonical_dataset, config, tmp_path / f"formal-{index}") for index in range(3)
    ]
    assert len({item.decision_hash for item in results}) == 1
    assert len({item.output_hash for item in results}) == 1
    assert all(item.fills for item in results)
