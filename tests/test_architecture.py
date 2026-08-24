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
    files = [ROOT / "pyproject.toml", *sorted((ROOT / "src").rglob("*.py"))]
    forbidden = ("apex_research", "apex-research", "apextrade", "leanadapter", "lean_adapter")
    for path in files:
        content = path.read_text(encoding="utf-8").lower()
        assert all(term not in content for term in forbidden), path


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
