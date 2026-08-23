import json
from pathlib import Path

import pytest

from markethub_nautilus.config import RunConfig

ROOT = Path(__file__).parents[2]


def test_s_config_is_strict_and_hashable() -> None:
    config = RunConfig.load(ROOT / "configs" / "s-validation.json")
    assert config.data.instruments == ("SH.600000", "SZ.000001")
    assert len(config.strategy.decisions) == 15
    assert len(config.config_hash) == 64
    assert len(config.strategy.spec_hash) == 64


def test_rule_overrides_require_explicit_opt_in(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "configs" / "s-validation.json").read_text())
    payload["strategy"]["allow_rule_overrides"] = False
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit"):
        RunConfig.load(path)
