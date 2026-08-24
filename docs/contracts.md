# Contracts

The canonical contracts are supplied by `strategy-workspace>=0.1.0,<0.2.0`; Quant Runtime does not
bundle copies.

| Workspace contract | Runtime use |
|---|---|
| `strategy-package.v1` / package ref | load verified package entrypoints from the registered tar ArtifactRef |
| `workspace-run-request.v2` | immutable package, parameters, snapshot, and execution topology |
| reference/materialized snapshot v1 | fail-closed MarketHub read or ArtifactRef materialization |
| runtime capability v1 | vocabulary represented by Runtime's concrete adapter profiles |
| run attempt / error / manifest | atomic worker lifecycle and evidence lineage |
| `result.v2` | canonical discovery, formal, comparison, and agreement output |

`result.v2.formal` is keyed by formal execution id, not adapter name. Each value reports its real
adapter plus scalar metrics. `discovery` is omitted for formal-only topologies. `comparison` contains
reference-free pairwise differences; agreement gates add selector values, tolerance policy, gate
status, and rejection details.

Workspace package registration produces a deterministic tar ArtifactRef. Runtime verifies and safely
extracts it, rejects special/path-traversing entries, and uses the hydrated package record rather than
the diagnostic source path. This keeps wheel installations independent of any source checkout.

Reference snapshot identity includes its full source/query and frozen `data_revision`. Runtime reads
MarketHub with that revision and rejects drift. Materialized metadata and partitions are ArtifactRefs;
Runtime materializes them through `WorkspaceClient`, verifies content identity, then reconstructs and
validates the canonical dataset.

The legacy `quant-runtime.candidate-manifest.v1`, `quant-runtime.formal-manifest.v1`, strategy spec,
golden result, and workspace-run-request.v1 contracts are no longer public or accepted.
