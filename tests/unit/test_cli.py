from __future__ import annotations

import json
from pathlib import Path

from quant_runtime import cli
from quant_runtime.application import ApplicationResult

ROOT = Path(__file__).parents[2]


def test_cli_discover_prints_frozen_stdout_shape(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "run_discover",
        lambda config, output: ApplicationResult(
            payload={
                "status": "passed",
                "run_id": "candidate-run",
                "manifest_path": str((tmp_path / "candidate_manifest.json").resolve()),
            },
            exit_code=0,
        ),
    )
    code = cli.main(
        [
            "discover",
            "--config",
            str(ROOT / "configs" / "discovery" / "s-momentum.json"),
            "--output",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert set(payload) == {"status", "run_id", "manifest_path"}


def test_cli_evaluate_and_golden_stdout_shapes(monkeypatch, tmp_path: Path, capsys) -> None:
    manifest_path = tmp_path / "formal_manifest.json"
    selected_runtime = None

    def evaluate(candidate, config, output, *, runtime_name):
        nonlocal selected_runtime
        selected_runtime = runtime_name
        return ApplicationResult(
            payload={
                "status": "matched",
                "run_id": "formal-run",
                "manifest_path": str(manifest_path),
            },
            exit_code=0,
        )

    monkeypatch.setattr(
        cli,
        "run_evaluate",
        evaluate,
    )
    code = cli.main(
        [
            "evaluate",
            "--candidate-manifest",
            "candidate.json",
            "--config",
            "formal.json",
            "--output",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert set(payload) == {"status", "run_id", "manifest_path"}
    assert selected_runtime == "nautilus"
    monkeypatch.setattr(
        cli,
        "run_golden_check",
        lambda candidate, formal, output: ApplicationResult(
            payload={
                "status": "matched",
                "candidate_run_id": "candidate-run",
                "formal_run_id": "formal-run",
                "report_path": str((tmp_path / "golden_check.json").resolve()),
            },
            exit_code=0,
        ),
    )
    code = cli.main(
        [
            "golden-check",
            "--candidate-manifest",
            "candidate.json",
            "--formal-manifest",
            str(tmp_path / "formal_manifest.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert set(payload) == {
        "status",
        "candidate_run_id",
        "formal_run_id",
        "report_path",
    }
    assert Path(payload["report_path"]) == (tmp_path / "golden_check.json").resolve()
