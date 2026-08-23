# Quant Runtime

Quant Runtime is one independent Python 3.12 product with framework adapters over the same
MarketHub data and strategy contracts:

- Qlib performs fast candidate discovery and emits native IC/risk evidence.
- NautilusTrader independently recomputes the strategy inside its event runtime and owns formal
  orders, fills, positions, account, fees, and statistics.

It can be used without Apex Research. MarketHub is its only production market-data source, and raw
bars remain in memory. Research control planes remain outside this repository and communicate only
through the CLI and neutral manifests, so Apex Research can be replaced or multiple research
systems can consume the same runtime evidence.

## Package layout

```text
src/quant_runtime/
├─ application/             # discover/evaluate/golden-check use-case orchestration
├─ contracts/               # neutral manifests, strategy spec, hashes, artifacts
├─ market_data/
│  └─ markethub/            # sole production data-source adapter + canonical dataset
├─ discovery/
│  └─ qlib/                 # Qlib-native discovery implementation
├─ formal/
│  ├─ interface.py          # neutral FormalRuntime seam
│  ├─ registry.py           # available formal runtime adapters
│  └─ nautilus/             # NautilusTrader-native formal implementation
└─ semantics/               # shared decision semantics and golden comparison
```

A future LEAN integration belongs beside Nautilus as `formal/lean/` and implements the same small
formal runtime seam. No LEAN placeholder, dependency, matcher, or execution model exists today.

## Commands

```powershell
uv sync --python 3.12 --extra dev

uv run quant-runtime discover `
  --config configs/discovery/qlib/s-momentum.json `
  --output runtime/discovery-1

uv run quant-runtime evaluate `
  --candidate-manifest runtime/discovery-1/candidate_manifest.json `
  --config configs/formal/nautilus/s-momentum.json `
  --output runtime/formal-1

# Optional today; the default remains nautilus.
uv run quant-runtime evaluate `
  --runtime nautilus `
  --candidate-manifest runtime/discovery-1/candidate_manifest.json `
  --config configs/formal/nautilus/s-momentum.json `
  --output runtime/formal-1

uv run quant-runtime golden-check `
  --candidate-manifest runtime/discovery-1/candidate_manifest.json `
  --formal-manifest runtime/formal-1/formal_manifest.json `
  --output runtime/golden-1
```

Every command prints a compact final JSON object. Discovery writes schema
`quant-runtime.candidate-manifest.v1`; formal evaluation writes
`quant-runtime.formal-manifest.v1`. `evaluate` uses the candidate only as a lineage and semantic
gate: it validates the MarketHub data version and canonical strategy hash, independently computes
runtime decisions from observed bars, and compares the resulting decision hash after execution.

The two config hashes cover the exact bytes of their supplied config files. Strategy and decision
hashes use canonical compact sorted JSON and remain independent of config formatting.
When `golden-check --output` is omitted, `golden_check.json` is written beside the formal manifest;
the final stdout object always contains `report_path`.

See [architecture](docs/architecture.md), [public manifest fields](docs/contracts.md), and
[connected validation evidence](docs/validation.md).

## Verification

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build
```
