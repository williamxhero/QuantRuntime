# Quant Runtime 0.2.1

Quant Runtime is the Qlib/Nautilus execution plane for
[`strategy-workspace`](../strategy-workspace). Strategy Workspace owns durable state and public
contracts; this package resolves real runtime capabilities, reads frozen MarketHub snapshots, runs
registered package entrypoints, and closes Workspace attempts atomically with canonical results and
engine-native evidence.

Production integrations are deliberately honest:

- MarketHub is the only market-data source.
- Qlib is the only discovery adapter.
- NautilusTrader is the only formal adapter.
- LEAN, ApexTrade, and Apex Research are not imported, registered, stubbed, or used as fallbacks.

## Installation

```powershell
uv sync --python 3.12 --extra dev
```

Wheel metadata uses the normal `strategy-workspace>=0.1.0,<0.2.0` dependency. Local development uses
the sibling checkout through `[tool.uv.sources]`; the built wheel contains neither sibling code nor a
Workspace implementation.

## CLI

The CLI only exposes run lifecycle operations and prints exactly one JSON document to stdout.
Usage and parse errors use the same JSON boundary; `--help` remains conventional text help.

```powershell
uv run quant-runtime run `
  --workspace D:\WILL\STOCK\QuantResearch\runtime\workspace `
  --request request.json

uv run quant-runtime run `
  --workspace D:\WILL\STOCK\QuantResearch\runtime\workspace `
  --package D:\path\to\strategy-package `
  --request request.json

uv run quant-runtime retry `
  --workspace D:\WILL\STOCK\QuantResearch\runtime\workspace `
  --request-id qrun_...
```

`--package` registers the package through `WorkspaceClient.register_package` and replaces the
request's package ref before submission. Repeating the same request returns the same request/run id.
A failed request is not retried implicitly; `retry` creates and executes a new attempt.

## Execution contract

The input is `quant-research.workspace-run-request.v2` from Strategy Workspace. It contains a
registered package ref, complete parameters, a full frozen reference or materialized MarketHub
snapshot, and one topology:

- `formal_only`
- `discovery_formal`
- `formal_comparison`
- `agreement_gate`

Formal comparison legs are keyed by their execution ids, so the same real Nautilus adapter can run
independent A/B configurations. Agreement gates compare declared scalar metric selectors with
absolute and relative tolerances; a failed gate ends as valid `rejected`, not as an execution error.

Reference snapshots verify the frozen MarketHub data and dataset revision before execution and keep
bars in memory when `local_cache=none`. Materialized snapshots consume only Workspace ArtifactRefs
and verify every hash, byte count, coverage partition, canonical input hash, calendar, and catalog.

See [architecture](docs/architecture.md), [contracts](docs/contracts.md),
[validation](docs/validation.md), and the [0.2.1 audit](docs/TODO.md).

Each Nautilus formal leg publishes `native_statistics.json` with schema
`quant-runtime.nautilus-reporting-input.v1`. It preserves the existing native statistics and adds
the public engine inputs required by downstream Strategy Reporting: general statistics, finite
timestamped portfolio returns, run metadata, account balances, extraction provenance, and explicit
availability reasons. Runtime does not render HTML and deliberately keeps the core dependency at
`nautilus_trader==1.231.0` without the visualization extra.

## Verification

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -m "not connected"
uv run pytest -m connected
uv build
git diff --check
```
