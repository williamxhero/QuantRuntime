# Contracts

All new contracts are bundled Draft 2020-12 JSON Schemas under `quant_runtime.schemas`:

| Schema | Purpose |
|---|---|
| `strategy-package.v1` | package identity, requirements, policy, and implementation entrypoints |
| `runtime-capability.v1` | exact adapter/engine versions and supported capabilities |
| `market-snapshot-ref.v1` | frozen logical MarketHub reference and trust policy |
| `market-snapshot.v1` | immutable materialized files and canonical input identity |
| `workspace-run-request.v1` | package, complete parameters, data, discovery, and formal topology |
| `run-manifest.v1` | complete run lineage and artifact integrity records |
| `result.v1` | canonical formal results, comparison, warnings, and incomparable items |
| `decision-intents.v2` | versioned neutral intent envelope for future cross-layer exchange |

The first package is `strategies/equity/cross-sectional-momentum`. Its content hash covers the TOML
manifest, parameter schema, declared discovery/formal implementation files, and explicitly declared
assets only. Tests, runtime state, and unrelated files do not change package identity.

Parameters follow one closed rule: no supplied object means the complete set of schema defaults;
supplying an object means it must itself be the complete valid object. Partial user objects are not
merged with defaults. Additional fields and missing fields are rejected.

Snapshot identity excludes local cache policy because cache is replaceable, but includes normalized
query semantics, endpoint contract, calendar and contract mapping, adapter version, trust policy, and
the available MarketHub data/dataset revision. Materialized identity additionally binds every source
file hash plus catalog, calendar, coverage, and canonical input hash.

Run identity binds all semantic inputs: package/parameter hashes, snapshot ID and source, full
normalized request topology/configuration, cache policy/read method, and selected capability profile
versions. Thus configuration or adapter/engine upgrades cannot reuse old evidence.

Formal inputs contain a package, complete parameters, resolved snapshot, output path, formal config,
and cache conversion details. They contain no discovery candidate. Each formal result preserves both
canonical positions/fills/account curve/metrics and engine-native evidence. Finite floating metrics
are compared deterministically; NaN and infinity fail closed as incomparable values.

The existing `quant-runtime.candidate-manifest.v1` and `quant-runtime.formal-manifest.v1` contracts
remain available through the legacy commands. Workspace schemas do not reinterpret or silently
migrate those artifacts.
