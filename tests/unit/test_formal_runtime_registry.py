from pathlib import Path

import pytest

from quant_runtime.application.evaluate import run_evaluate
from quant_runtime.formal.registry import formal_runtime_names, get_formal_runtime


def test_registry_exposes_only_the_real_nautilus_adapter() -> None:
    assert formal_runtime_names() == ("nautilus",)
    runtime = get_formal_runtime("nautilus")
    assert runtime.name == "nautilus"
    assert callable(runtime.evaluate)
    with pytest.raises(ValueError, match="unsupported formal runtime"):
        get_formal_runtime("lean")


def test_application_evaluate_uses_the_neutral_runtime_seam(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class Runtime:
        name = "test"

        def evaluate(self, candidate_manifest: Path, config: Path, output: Path):
            calls.append((candidate_manifest, config, output))
            return (
                {"status": "matched", "run_id": "formal-run"},
                output / "formal_manifest.json",
            )

    monkeypatch.setattr(
        "quant_runtime.application.evaluate.get_formal_runtime",
        lambda name: Runtime(),
    )
    result = run_evaluate(
        Path("candidate.json"),
        Path("formal.json"),
        tmp_path,
        runtime_name="test",
    )
    assert calls == [(Path("candidate.json"), Path("formal.json"), tmp_path)]
    assert result.payload == {
        "status": "matched",
        "run_id": "formal-run",
        "manifest_path": str(tmp_path / "formal_manifest.json"),
    }
    assert result.exit_code == 0
