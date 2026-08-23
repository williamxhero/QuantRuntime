# MarketHub Qlib workspace

An independent, Python 3.12 workspace for fast A-share discovery with upstream
Qlib. It reads MarketHub directly, fails closed on version or completeness
violations, and emits native Qlib evaluation output plus a hashed run manifest.

It is intentionally not a formal execution engine. Orders, fills, accounts,
settlement, and final research decisions belong elsewhere.

## Setup

```powershell
uv sync --extra dev
```

## Run

```powershell
uv run python -m markethub_qlib.cli run `
  --config configs/s-smoke.json `
  --output runtime/s-smoke
```

The production CLI always reads MarketHub. There is no local-price fallback.
The final stdout line is a compact JSON object containing `status`, `run_id`,
and `manifest_path`.

Each successful run also writes `strategy_spec.json` and
`strategy_decisions.json`. These are neutral, pre-matching golden-contract
artifacts; they contain no fills or precomputed execution results.

`run_manifest.json.config_hash` is the SHA-256 of the exact `--config` file
bytes. Reformatting the JSON intentionally changes the run identity while the
canonical `strategy_spec_hash` remains unchanged.

## Verify

```powershell
uv run pytest
uv run pytest --run-connected -m connected
uv run ruff check .
```

The connected S smoke is two stocks over January 2025. A full-A scale claim is
outside this workspace's initial acceptance gate and must not be inferred from
the S smoke.
