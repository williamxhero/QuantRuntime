from __future__ import annotations

from pathlib import Path

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
