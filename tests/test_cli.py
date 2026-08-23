from __future__ import annotations

import json
from pathlib import Path

from markethub_qlib import cli
from markethub_qlib.client import MarketHubClient
from markethub_qlib.config import RunConfig
from markethub_qlib.workflow import run_discovery

ROOT = Path(__file__).parents[1]


def test_cli_prints_machine_readable_final_line(
    fixture_client: MarketHubClient,
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    config_path = ROOT / "configs" / "s-smoke.json"
    config = RunConfig.load(config_path)
    result = run_discovery(config, client_factory=lambda _: fixture_client)
    monkeypatch.setattr(cli, "run_discovery", lambda _: result)

    exit_code = cli.main(["run", "--config", str(config_path), "--output", str(tmp_path)])
    lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(lines[-1])
    assert exit_code == 0
    assert set(payload) == {"status", "run_id", "manifest_path"}
    assert payload["status"] == "passed"
    assert Path(payload["manifest_path"]).is_file()
