from __future__ import annotations

import json
from pathlib import Path

from quant_runtime import cli

ROOT = Path(__file__).parents[2]


def test_cli_discover_prints_frozen_stdout_shape(monkeypatch, tmp_path: Path, capsys) -> None:
    class Result:
        pass

    monkeypatch.setattr(cli, "run_discovery", lambda config: Result())
    monkeypatch.setattr(
        cli,
        "write_candidate_run",
        lambda config, result, output: (
            {"status": "passed", "run_id": "candidate-run"},
            (tmp_path / "candidate_manifest.json").resolve(),
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
    monkeypatch.setattr(
        cli,
        "evaluate_candidate",
        lambda candidate, config, output: (
            {"status": "matched", "run_id": "formal-run"},
            manifest_path,
        ),
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
    monkeypatch.setattr(
        cli,
        "compare_manifests",
        lambda candidate, formal: {
            "status": "matched",
            "semantic_match": True,
            "candidate_run_id": "candidate-run",
            "formal_run_id": "formal-run",
        },
    )
    monkeypatch.setattr(
        cli,
        "write_golden_report",
        lambda output, report: (output / "golden_check.json").resolve(),
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
