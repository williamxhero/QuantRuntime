# Validation

Offline tests cover all four topologies, a valid rejected agreement gate, result.v2 shape, package tar
materialization, request idempotency, explicit retry attempts, runtime identity, Workspace artifact
lineage, strict JSON CLI output, real Nautilus evidence, observed-bar decisions, MarketHub version
drift, incomplete/out-of-order delivery, materialized ArtifactRefs, wheel metadata, and deleted legacy
ownership.

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
configs, candidate/formal manifests, or build/runtime state.
