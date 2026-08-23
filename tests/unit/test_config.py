import json
from hashlib import sha256
from pathlib import Path

import pytest

from markethub_nautilus.config import RunConfig

ROOT = Path(__file__).parents[2]


def test_s_config_is_strict_and_hashable() -> None:
    path = ROOT / "configs" / "s-validation.json"
    config = RunConfig.load(path)
    assert config.data.instruments == ("SH.600000", "SZ.000001")
    assert len(config.strategy.decisions) == 15
    assert len(config.config_hash) == 64
    assert config.config_hash == sha256(path.read_bytes()).hexdigest()
    assert len(config.strategy.spec_hash) == 64


def test_formal_momentum_config_uses_neutral_strategy_spec() -> None:
    config = RunConfig.load(ROOT / "configs" / "cross-sectional-momentum-topk.s.json")
    assert config.strategy.spec_payload == {
        "strategy_id": "cross-sectional-momentum-topk",
        "spec_revision": "1",
        "parameters": {"lookback_days": 3, "top_k": 1},
    }
    assert (
        config.strategy.spec_hash
        == "f06669db3f35dd2096456df51fb69707dea3fd50d53d828bdaff8e7833bccd6d"
    )


def test_rule_overrides_require_explicit_opt_in(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "configs" / "s-validation.json").read_text())
    payload["strategy"]["allow_rule_overrides"] = False
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit"):
        RunConfig.load(path)
