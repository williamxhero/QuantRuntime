from pathlib import Path

from markethub_nautilus.manifest import write_manifest


def test_manifest_hashes_artifacts_and_has_required_identity(tmp_path: Path) -> None:
    artifact = tmp_path / "native_statistics.json"
    artifact.write_text("{}\n", encoding="utf-8")
    manifest, path = write_manifest(
        tmp_path,
        framework_version="1.231.0",
        status="success",
        data_version="v1",
        config_hash="a" * 64,
        strategy_spec_hash="b" * 64,
        canonical_input_hash="c" * 64,
        normalized_output_hash="d" * 64,
        artifact_paths=[artifact],
    )
    assert path.name == "run_manifest.json"
    assert manifest["schema"] == "markethub-nautilus.run-manifest.v1"
    assert manifest["tool_version"] == "0.1.0"
    assert manifest["artifacts"][0]["relative_path"] == artifact.name
    assert manifest["artifacts"][0]["content_bytes"] == len(artifact.read_bytes())
    assert len(manifest["artifacts"][0]["sha256"]) == 64
