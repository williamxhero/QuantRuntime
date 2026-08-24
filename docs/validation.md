# Validation

Offline tests cover all four topologies, a valid rejected agreement gate, result.v2 shape, package tar
materialization, request idempotency, explicit retry attempts, runtime identity, Workspace artifact
lineage, strict JSON CLI output, real Nautilus evidence, observed-bar decisions, MarketHub version
drift, incomplete/out-of-order delivery, materialized ArtifactRefs, wheel metadata, and deleted legacy
ownership.

Reporting-input tests additionally lock the exact Nautilus 1.231.0 public Interface, equity/futures
artifact parity, empty native reports, short and empty return series, UTC ordering, duplicate
timestamps, non-finite rejection, explicit native-statistic unavailability, artifact record schema,
and normalized-output parity. They test saved offline inputs only and never import or call the
visualization renderer.

The connected gate uses the live MarketHub service on the small computer and remains fail closed. A
service outage, unpublished dataset, incomplete coverage, or version drift is an operational result,
not a reason to substitute fixture data. Repository structure and offline functionality do not wait
for live data readiness.

Run:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -m "not connected"
uv run pytest -m connected
uv build
git diff --check
```

After `uv build`, inspect the wheel to confirm it includes `quant_runtime` only, declares the normal
Strategy Workspace version constraint, and contains no Workspace schemas/storage, strategies,
configs, candidate/formal manifests, or build/runtime state. In a fresh non-editable environment,
import `quant_runtime.adapters.formal.nautilus.reporting_input`, verify schema
`quant-runtime.nautilus-reporting-input.v1`, and decode a saved artifact with the installed wheel.
