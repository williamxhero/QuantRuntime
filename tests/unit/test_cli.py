import json
from pathlib import Path

from markethub_nautilus import cli


def test_cli_stdout_finishes_with_machine_readable_result(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "run_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    def fake_run(config_path, output_dir):
        return {"status": "success", "run_id": "nt-" + "a" * 24}, manifest_path

    monkeypatch.setattr(cli, "run", fake_run)
    result = cli.main(["run", "--config", "input.json", "--output", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 0
    assert payload == {
        "manifest_path": str(manifest_path.resolve()),
        "run_id": "nt-" + "a" * 24,
        "status": "success",
    }
