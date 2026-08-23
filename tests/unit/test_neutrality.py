from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_production_source_and_dependencies_are_neutral() -> None:
    files = [ROOT / "pyproject.toml", *sorted((ROOT / "src").rglob("*.py"))]
    forbidden = ("apex_research", "Apex Research")
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert all(term not in content for term in forbidden), path
