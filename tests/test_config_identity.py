from __future__ import annotations

import hashlib
import json
from pathlib import Path

import qlib

from markethub_qlib.artifacts import write_failed_run
from markethub_qlib.canonical import sha256_value
from markethub_qlib.config import RunConfig

ROOT = Path(__file__).parents[1]


def test_equivalent_json_formatting_changes_config_hash(tmp_path: Path) -> None:
    source_path = ROOT / "configs" / "s-smoke.json"
    source = RunConfig.load(source_path)
    compact_path = tmp_path / "compact.json"
    compact_path.write_text(
        json.dumps(source.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    compact = RunConfig.load(compact_path)

    assert source.raw == compact.raw
    assert source.config_hash != compact.config_hash
    assert sha256_value(source.strategy_spec) == sha256_value(compact.strategy_spec)
    assert source.config_hash == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert compact.config_hash == hashlib.sha256(compact_path.read_bytes()).hexdigest()


def test_failed_manifest_and_run_id_use_file_byte_hash(tmp_path: Path) -> None:
    source_path = ROOT / "configs" / "s-smoke.json"
    source = RunConfig.load(source_path)
    compact_path = tmp_path / "compact.json"
    compact_path.write_text(
        json.dumps(source.raw, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    compact = RunConfig.load(compact_path)

    source_run = write_failed_run(
        source,
        tmp_path / "source-run",
        RuntimeError("expected failure"),
        framework_version=qlib.__version__,
    )
    compact_run = write_failed_run(
        compact,
        tmp_path / "compact-run",
        RuntimeError("expected failure"),
        framework_version=qlib.__version__,
    )
    source_manifest = json.loads(source_run.manifest_path.read_text(encoding="utf-8"))
    compact_manifest = json.loads(compact_run.manifest_path.read_text(encoding="utf-8"))

    assert source_manifest["config_hash"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert compact_manifest["config_hash"] == hashlib.sha256(compact_path.read_bytes()).hexdigest()
    assert source_manifest["strategy_spec_hash"] == compact_manifest["strategy_spec_hash"]
    assert source_run.run_id != compact_run.run_id
