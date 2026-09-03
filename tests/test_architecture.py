from __future__ import annotations

from pathlib import Path

import pytest

from quant_runtime.entrypoint import load_package_entrypoint
from quant_runtime.package import StrategyPackage
from quant_runtime.registry import production_registry

ROOT = Path(__file__).parents[1]


def test_production_registry_has_only_real_adapters_and_no_apex_import() -> None:
    registry = production_registry()
    assert registry.names("discovery") == ("qlib",)
    assert registry.names("formal") == ("nautilus",)
    nautilus = registry.profile("formal", "nautilus")
    assert nautilus.adapter_version == "1.1.1"
    assert "evidence.nautilus_reporting_input" in nautilus.capabilities
    files = [ROOT / "pyproject.toml", *sorted((ROOT / "src").rglob("*.py"))]
    forbidden = ("apex_research", "apex-research", "apextrade", "leanadapter", "lean_adapter")
    for path in files:
        content = path.read_text(encoding="utf-8").lower()
        assert all(term not in content for term in forbidden), path


def test_runtime_does_not_add_a_private_control_plane_or_reporting_owner() -> None:
    files = [ROOT / "pyproject.toml", *sorted((ROOT / "src").rglob("*.py"))]
    forbidden = (
        "sqlite3",
        "strategy_workspace.storage",
        "strategy_workspace.core",
        "apex_research",
        "strategy_reporting",
    )
    for path in files:
        content = path.read_text(encoding="utf-8").lower()
        assert all(term not in content for term in forbidden), path
        if path.name != "oci.py":
            assert "subprocess" not in content, path
    oci = (ROOT / "src" / "quant_runtime" / "sandbox" / "oci.py").read_text(encoding="utf-8")
    assert "shell=True" not in oci
    assert "shell=False" in oci


def test_legacy_workspace_and_manifest_ownership_is_gone() -> None:
    source = ROOT / "src" / "quant_runtime"
    # Ignored interpreter caches can survive a source-tree migration locally.  The
    # ownership boundary is about shipped source and bundled contracts, not those
    # disposable cache directories.
    assert not list((source / "workspace").rglob("*.py"))
    assert not list((source / "schemas").rglob("*.json"))
    assert not (source / "contracts" / "candidate_manifest.py").exists()
    assert not (source / "contracts" / "formal_manifest.py").exists()
    assert not list((ROOT / "strategies").rglob("strategy.toml"))


def test_nautilus_reporting_input_uses_no_private_or_visualization_fallbacks() -> None:
    source = (
        ROOT / "src" / "quant_runtime" / "adapters" / "formal" / "nautilus" / "reporting_input.py"
    ).read_text(encoding="utf-8")
    assert "analyzer._" not in source
    assert "account._" not in source
    assert "create_tearsheet" not in source
    assert "plotly" not in source.lower()
    assert "kaleido" not in source.lower()

    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"nautilus_trader==1.231.0"' in project
    assert "nautilus_trader[visualization]" not in project


def test_strategy_package_requires_an_explicit_materialized_root(tmp_path: Path) -> None:
    source_path = tmp_path / "diagnostic-source"
    source_path.mkdir()
    record = {
        "package_ref": {"strategy_id": "fixture", "revision": 1, "package_hash": "a" * 64},
        "manifest": {"strategy_id": "fixture", "revision": 1},
        "source_path": str(source_path),
    }

    with pytest.raises(TypeError, match="root"):
        StrategyPackage.from_record(record)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="not materialized"):
        StrategyPackage.from_record(record, root=tmp_path / "missing")


def test_package_entrypoint_supports_python_dataclasses(tmp_path: Path) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class StrategyConfig:\n"
        "    lookback: int = 20\n",
        encoding="utf-8",
    )

    config_class = load_package_entrypoint(tmp_path, "strategy.py:StrategyConfig")

    assert config_class().lookback == 20
