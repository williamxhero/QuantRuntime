from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_production_source_and_dependencies_have_no_control_plane_import() -> None:
    files = [ROOT / "pyproject.toml", *sorted((ROOT / "src").rglob("*.py"))]
    forbidden = ("apex_research", "apex-research")
    for path in files:
        content = path.read_text(encoding="utf-8").lower()
        assert all(term not in content for term in forbidden), path
