# Strategy Workspace implementation audit

This is the item-by-item audit for `完美的结构.md` under the frozen v1 delivery scope.

| Requirement | Status | Evidence |
|---|---|---|
| Evolve in place; preserve repo/package and v1 workflow | Complete | `quant-runtime` unchanged; legacy contract/CLI tests |
| Public `validate_package`, `resolve_snapshot`, `run` | Complete | `quant_runtime.workspace` and CLI tests |
| `sdk/workspace/adapters/data\|discovery\|formal` roles | Complete | source topology and topology tests |
| TOML package, Draft 2020-12 schemas, full parameter rule | Complete | package/schema contract tests |
| Package content hash with declared implementation/assets scope | Complete | package hash regressions |
| Exact capability registry and four selection modes | Complete | capability/topology tests; ambiguity fails closed |
| Qlib only real discovery; Nautilus only real formal | Complete | production registry and topology test |
| No fake LEAN/ApexTrade integration or empty production dirs | Complete | filesystem/registry topology tests |
| Formal-neutral input; no candidate; formal-only | Complete | neutral interface and formal-only tests |
| Independent multi-formal comparison after execution | Complete | adapter comparison tests; no reference backend |
| Reference revision freeze and drift failure | Complete | snapshot metadata/drift regressions |
| Materialized original monthly bars+coverage bytes | Complete | real manifest-shape fixture and integrity tests |
| Atomic snapshots with catalog/calendar/coverage | Complete | snapshot staging/contract tests |
| `none`, `ephemeral`, `persistent` cache behavior | Complete | observable lifecycle and reuse tests |
| Cache non-authoritative and actually consumed | Complete | reconstruction hash and Nautilus metrics tests |
| Momentum extracted as first Strategy Package | Complete | package entrypoints and legacy import regression |
| Eight requested v1/v2 schemas bundled | Complete | schema enumeration and wheel build audit |
| CLI package validation, snapshot resolution, run | Complete | CLI tests |
| `.runtime` layout and uncommitted outputs | Complete | layout tests and `.gitignore` |
| Direct `pyarrow` and `jsonschema` dependencies | Complete | `pyproject.toml` and lockfile |
| Contract/snapshot/topology/adapter/legacy/scale coverage | Complete | categorized test suite |
| Connected coverage | Implemented; external gate blocked | current MarketHub dataset publication is `not_ready` |
| Apex Research unchanged | Complete | no cross-project write/import |

The remaining connected failure is external operational readiness, not an unchecked repository TODO.
The implementation intentionally does not include the design document's future ApexTrade/LEAN,
second futures package, multiple discovery, or Apex Research migration work because those items were
explicitly outside the frozen delivery scope and would require real integrations rather than stubs.
