# Architecture

The workspace has three boundaries:

1. `MarketHubClient` freezes the health vector, reads catalog/calendar/daily
   windows, validates the transport contract, and returns an in-memory Qlib
   `DataFrame`.
2. `workflow.run_discovery` uses upstream Qlib signal evaluation to score a
   simple cross-sectional momentum experiment and select Top-K candidates.
3. `artifacts` writes deterministic evaluation exports and a content-addressed
   `markethub-qlib.run-manifest.v1` manifest.

## Neutral strategy contract

`strategy_spec.json` contains only `strategy_id`, `spec_revision`, and the
`lookback_days`/`top_k` parameters. Its canonical SHA-256 is the manifest's
top-level `strategy_spec_hash`.

`strategy_decisions.json` uses schema `canonical-strategy-decisions.v1` and
contains the spec hash plus pre-matching decision rows. Each row has
`signal_date`, `instrument`, and a canonical decimal-string `target_weight`.
Rows are ordered by date, then source score descending and instrument ascending;
suspended rows are excluded. The SHA-256 of the canonical JSON envelope is
published as `manifest.metrics.reference_decision_hash`. No fills, positions,
fees, or future-return labels enter this contract.

## Configuration identity

The manifest's `config_hash` is the SHA-256 of the exact bytes supplied through
`--config`. Whitespace, key order, encoding bytes, or a trailing newline are
therefore identity-bearing even when two files parse to the same JSON object.
Successful and failed manifests, as well as `run_id`, use this same byte hash.
`strategy_spec_hash` remains a separate canonical-JSON semantic hash.

MarketHub data is never written as a reusable local data copy. Qlib evaluation
exports contain only signals, labels, IC series, candidates, and summary
metrics. The production dependency direction ends at Qlib and MarketHub; this
repository has no control-plane dependency.

## Fail-closed contract

- Freeze both `/api/health.data_version` and
  `dataset_versions.stock_daily_1d` before reading.
- Catalog pages must be unique; calendars must be strictly increasing.
- Daily rows must be globally ordered by `(trade_time, code)` with no duplicate.
- Every page must match the frozen versions, report no truncation, and satisfy
  all completeness flags and full per-code coverage.
- Cursors are opaque, non-empty, and may not repeat.
- The health vector is read again after delivery. Any drift invalidates the run.
