# Architecture

Quant Runtime 0.2.1 is an execution worker, not a second control plane.

```text
Strategy Workspace
  package bundle + request.v2 + frozen snapshot
                     |
                     v
              RuntimeExecutor
       capability resolution + identity
          /                         \
 optional Qlib discovery       Nautilus formal leg(s)
          \                         /
           result.v2 + native evidence
                     |
                     v
WorkspaceWorker complete / reject / fail attempt
```

## Ownership boundary

Strategy Workspace owns registration, JSON Schemas, parameter validation, request identity, SQLite,
artifact bytes, run records, attempts, retry, publications, and canonical result validation. Runtime
accesses those facilities only through `WorkspaceClient` and `WorkspaceWorker`.

Runtime owns engine capability profiles, deterministic topology resolution, MarketHub consumption,
Qlib and Nautilus adapters, native engine execution, comparison calculations, runtime identity, and
evidence production. Its temporary adapter storage is attempt-scoped scratch, never a competing
Workspace registry or artifact store.

The former `quant_runtime.workspace`, bundled Workspace schemas, package-admin CLI, candidate/formal
manifests, and `discover`/`evaluate`/`golden-check` pipeline were deleted. Runtime never reads legacy
runtime data. Existing data is intentionally left on disk for external retention or migration.

## Runtime identity and attempts

Workspace `run_id` is the canonical `request_id`. Before engine work, `RuntimeExecutor` binds an
identity containing the request hash, package ref and parameter hash, full snapshot source/query,
topology, per-leg config hash, adapter and engine versions, and data read semantics. Output locations
and attempt scratch paths are excluded.

The first execution claims the submitted attempt. `completed` and `rejected` are idempotent terminal
states. Any exception is recorded with `fail_attempt`; Runtime never silently falls back or creates a
retry. `WorkspaceClient.retry_run` is the only path to a fresh attempt for the same immutable request.

## Adapter seams

`adapters/data/markethub`, `adapters/discovery/qlib`, and `adapters/formal/nautilus` contain the actual
integrations and their engine-facing models. Legacy parallel `market_data`, `discovery`, and `formal`
trees no longer exist. Package entrypoints are loaded from verified Workspace package tar artifacts.
The package loader requires the executor to pass that explicit materialized directory; it never
falls back to Workspace's diagnostic `source_path` field.

Qlib discovery remains optional and produces only native discovery evidence. Every formal leg starts
from the same frozen snapshot and independently owns orders, fills, positions, account state, fees,
market rules, and reports. Nautilus strategies derive decisions inside observed-bar callbacks; Qlib
candidate rows are absent from `FormalRunInput`.

Reference reads default to no raw-bar cache; an ephemeral non-authoritative conversion is available
when requested. A persistent conversion is rejected until the request supplies a Workspace-managed
ArtifactRef, so attempt scratch is never mislabeled as durable state.

## Reporting evidence seam

Both equity and futures runners call one shared extractor before `BacktestEngine.dispose()`. The
extractor uses only pinned Nautilus 1.231.0 public interfaces: `BacktestResult`,
`PortfolioAnalyzer.get_performance_stats_general()`, `PortfolioAnalyzer.portfolio_returns()`, and
the account balance methods. In this pinned release `BacktestResult` does not expose
`stats_general`, so the tested public analyzer method is the only general-statistics source.

The extractor never reads analyzer/account private attributes and never derives returns or account
metrics from reports, positions, or balance snapshots. Strategy Reporting is a separate downstream
read-model owner and consumes the immutable Workspace artifact; Runtime has no visualization
dependency and does not render a tearsheet.
