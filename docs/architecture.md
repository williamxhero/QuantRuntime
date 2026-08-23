# Architecture

The workspace has three boundaries:

1. `MarketHubClient` freezes the health vector, reads catalog/calendar/daily
   windows, validates the transport contract, and returns an in-memory Qlib
   `DataFrame`.
2. `workflow.run_discovery` uses upstream Qlib signal evaluation to score a
   simple cross-sectional momentum experiment and select Top-K candidates.
3. `artifacts` writes deterministic evaluation exports and a content-addressed
   `markethub-qlib.run-manifest.v1` manifest.

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
