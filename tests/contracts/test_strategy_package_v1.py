from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from quant_runtime.sdk.package_manifest import validate_package

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "strategies" / "equity" / "cross-sectional-momentum"


def test_defaults_or_full_user_parameter_object_are_the_only_valid_forms() -> None:
    package = validate_package(PACKAGE)
    defaults = package.resolve_parameters()
    assert defaults["lookback_days"] == 3
    with pytest.raises(ValueError, match="required property"):
        package.resolve_parameters({"lookback_days": 5})
    full = {**defaults, "lookback_days": 5}
    assert package.resolve_parameters(full) == full


def test_package_hash_covers_only_declared_package_content(tmp_path: Path) -> None:
    copied = tmp_path / "strategy"
    shutil.copytree(PACKAGE, copied)
    before = validate_package(copied).package_hash
    (copied / "tests" / "runtime-note.txt").write_text("not declared", encoding="utf-8")
    assert validate_package(copied).package_hash == before
    implementation = copied / "discovery" / "qlib" / "pipeline.py"
    implementation.write_text(implementation.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert validate_package(copied).package_hash != before
