from quant_runtime.cli import build_parser
from quant_runtime.contracts.candidate_manifest import CANDIDATE_SCHEMA
from quant_runtime.contracts.formal_manifest import FORMAL_SCHEMA


def test_v1_commands_and_manifest_schemas_remain_public() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    assert {"discover", "evaluate", "golden-check"} <= set(subparsers.choices)
    assert CANDIDATE_SCHEMA == "quant-runtime.candidate-manifest.v1"
    assert FORMAL_SCHEMA == "quant-runtime.formal-manifest.v1"
