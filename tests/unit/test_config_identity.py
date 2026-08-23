from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quant_runtime.discovery.workflow import DiscoveryConfig
from quant_runtime.formal.runner import FormalConfig

ROOT = Path(__file__).parents[2]


def test_discovery_config_hash_is_exact_file_bytes(tmp_path: Path) -> None:
    source_path = ROOT / "configs" / "discovery" / "s-momentum.json"
    source = DiscoveryConfig.load(source_path)
    compact_path = tmp_path / "compact.json"
    raw = json.loads(source_path.read_text())
    raw["strategy_spec"] = str(
        (ROOT / "configs" / "strategies" / "cross-sectional-momentum-topk.json").resolve()
    )
    compact_path.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
    compact = DiscoveryConfig.load(compact_path)
    assert source.config_hash == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert compact.config_hash == hashlib.sha256(compact_path.read_bytes()).hexdigest()
    assert source.config_hash != compact.config_hash
    assert source.strategy.spec_hash == compact.strategy.spec_hash


def test_formal_config_hash_is_exact_file_bytes() -> None:
    path = ROOT / "configs" / "formal" / "s-momentum.json"
    config = FormalConfig.load(path)
    assert config.config_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert config.lot_size == 100
