# Architecture

Quant Runtime evolves in place as a neutral Strategy Workspace. The repository and Python package
names remain unchanged, and the legacy workflow remains a supported compatibility surface.

```text
Strategy Package + complete parameters + MarketHub request
                         |
                  Strategy Workspace
       validate_package / resolve_snapshot / run
                         |
             +-----------+-----------+
             |                       |
      Qlib discovery            Nautilus formal
      (optional, lineage)       (sole state owner)
             |                       |
             +-----------+-----------+
                         |
          canonical result + native evidence
```

`sdk/` owns package, parameter, capability, snapshot, decision-intent, run, and result contracts.
`workspace/` owns validation, deterministic routing, atomic publication, run identity, comparison,
and evidence indexing. `adapters/` contains role-specific production integrations. The production
registry declares exactly MarketHub for data, Qlib for discovery, and NautilusTrader for formal
execution. Synthetic adapters exist only in tests.

No formal backend is a hidden fallback. `pinned`, `capability_match`, `comparison`, and
`agreement_gate` resolve against exact capability profiles. Matching ambiguity fails unless the
request supplies a preference. Every formal backend in a comparison receives the same neutral
snapshot and runs independently; comparison happens after formal execution and has no reference
backend. Formal inputs deliberately cannot contain a Qlib candidate.

## Data authority

MarketHub remains the sole production authority. A reference snapshot performs a metadata-only
health read to freeze both MarketHub data and `stock_daily_1d` dataset versions. It reads no bars
during resolution. `assumed_immutable` describes byte-level trust, not an unknown revision; a later
bar read must match the frozen revision or fail. `verified_immutable` is emitted only after a full
validated read.

A materialized snapshot uses the published export mapping and manifest. It downloads only requested
months' original bars and coverage Parquet bytes, verifies manifest/file hashes, byte and row counts,
schema, catalog, calendar, and coverage, then atomically publishes an immutable content-addressed
snapshot. Staging is never authoritative.

Local cache policy is independent of snapshot authority. `none` writes no cache; `ephemeral` creates
a verified canonical conversion under staging and removes it after the consumer returns;
`persistent` stores a content-addressed verified conversion under `.runtime/cache`. The Nautilus
adapter actually reconstructs and consumes non-`none` caches. Cache manifests record the source
snapshot, canonical input hash, transform version, file hashes, and `authoritative: false`.

## Identity and compatibility

Workspace run identity binds the package and complete parameter hashes, frozen snapshot and source
revision, normalized request semantics (including discovery config and cache policy), read method,
and selected data/discovery/formal adapter and engine capability versions. Only output placement and
runtime root are excluded. Artifacts use relative paths, SHA-256, and byte counts.

The extracted momentum Strategy Package owns its Qlib and Nautilus entrypoints. The former core
strategy import remains a compatibility shim. Legacy `discover`, `evaluate`, and `golden-check`
commands and candidate/formal v1 manifests are unchanged.

Apex Research is external and untouched. LEAN and ApexTrade are neither connected nor represented by
empty production directories. Future adapters must enter through the same role contract only when a
real, tested integration exists.
