# Quant Runtime

Quant Runtime is an independent Python 3.12 Strategy Workspace. It validates versioned strategy
packages, freezes MarketHub input identity, resolves an explicit execution topology, and preserves
canonical results beside engine-native evidence.

The production registry is deliberately honest and small:

- MarketHub is the only production market-data adapter.
- Qlib is the only real discovery adapter.
- NautilusTrader is the only real formal backend.
- ApexTrade and LEAN are not installed, registered, stubbed, or treated as fallback engines.
- Apex Research remains an external consumer; this repository neither imports nor modifies it.

Legacy `discover`, `evaluate`, and `golden-check` commands and their v1 manifests remain supported.

## Layout

```text
src/quant_runtime/
├─ sdk/                    # package, parameter, capability, snapshot, run and result contracts
├─ schemas/                # bundled Draft 2020-12 JSON Schemas
├─ workspace/              # validate_package / resolve_snapshot / run
├─ adapters/
│  ├─ data/markethub/      # reference/materialized snapshots and cache policy
│  ├─ discovery/qlib/      # only real discovery adapter
│  └─ formal/nautilus/     # only real formal backend
├─ application/            # new and legacy CLI-neutral use cases
├─ contracts/              # legacy v1 manifest compatibility
├─ discovery/qlib/         # legacy Qlib workflow compatibility
├─ formal/nautilus/        # legacy Nautilus workflow and shared public seams
└─ semantics/              # legacy decision and golden contracts

strategies/equity/cross-sectional-momentum/
├─ strategy.toml
├─ parameters.schema.json
├─ discovery/qlib/pipeline.py
└─ formal/nautilus/strategy.py
```

Runtime state is never committed:

```text
.runtime/
├─ snapshots/              # immutable reference manifests or materialized authorities
├─ runs/                   # run manifests, canonical result and engine-native evidence
├─ evidence/               # run evidence indexes
├─ cache/                  # explicitly requested non-authoritative cache
└─ staging/                # atomic publication staging
```

## Strategy Workspace commands

```powershell
uv sync --python 3.12 --extra dev

uv run quant-runtime package-validate `
  --package strategies/equity/cross-sectional-momentum

# Resolves only the data section. Default reference/assumed reads version metadata, not bars.
uv run quant-runtime snapshot-resolve `
  --request configs/workspace/s-momentum.json `
  --runtime-root .runtime

uv run quant-runtime run `
  --request configs/workspace/s-momentum.json `
  --runtime-root .runtime
```

User parameters are frozen as one complete closed object. Omitting `--parameters` uses the complete
schema defaults; supplying a partial override is rejected instead of being silently merged.

Formal selection supports `pinned`, `capability_match`, `comparison`, and `agreement_gate`.
Capability matching never falls back to registry order. Comparison backends run independently on
the same snapshot, and comparison occurs only after every formal run has completed. A formal input
contains no Qlib candidate.

## Snapshot and cache policy

`snapshot_mode` and `local_cache` are independent:

- `reference` stores a logical MarketHub identity and freezes the available MarketHub data and daily
  dataset revisions through a metadata-only health read. Its default trust is `assumed_immutable`;
  the stronger `verified_immutable` label is emitted only after a real fail-closed bar read.
- `materialized` resolves MarketHub's real `stock_daily_1d` export mapping and manifest, downloads
  only intersecting monthly `bars.parquet` and `coverage.parquet` files, verifies manifest bytes,
  file bytes, row counts, SHA-256 and Parquet schema, and publishes atomically.
- `none` leaves no cache data.
- `ephemeral` is consumed from staging and deleted after the backend run while retaining its hash
  manifest.
- `persistent` stores and reuses a content-addressed conversion with transform version and hash.
  Nautilus reconstructs and verifies the exact canonical input from it. It is always marked
  `authoritative: false`; only the snapshot is authoritative.

## Legacy commands

```powershell
uv run quant-runtime discover `
  --config configs/discovery/qlib/s-momentum.json `
  --output runtime/discovery-1

uv run quant-runtime evaluate `
  --candidate-manifest runtime/discovery-1/candidate_manifest.json `
  --config configs/formal/nautilus/s-momentum.json `
  --output runtime/formal-1

uv run quant-runtime golden-check `
  --candidate-manifest runtime/discovery-1/candidate_manifest.json `
  --formal-manifest runtime/formal-1/formal_manifest.json `
  --output runtime/golden-1
```

See [architecture](docs/architecture.md), [contracts](docs/contracts.md),
[validation](docs/validation.md), and the [implementation audit](docs/TODO.md).

## Verification

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -m "not connected"
uv run pytest -m connected
uv build
```

Connected tests never replace a MarketHub outage or unpublished dataset with fixtures. Offline
snapshot contract tests use captured manifest shape and generated Parquet bytes.
