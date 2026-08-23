# Validation evidence and limits

Offline verification is authoritative for repository behavior. It covers JSON Schema/package
contracts, parameter and package hashes, real MarketHub export-manifest shape, reference drift,
materialized byte/hash/coverage integrity, atomic cleanup, all cache policies and actual cache
consumption, capability routing, formal-only and discovery+formal topology, post-formal comparison,
legacy commands/manifests, connected test definitions, and scale/determinism checks.

The captured export fixture matches the real `stock_daily_1d` manifest contract:
`schema_version`, dataset/data versions, range, compression, partition count, and
`files[{path,url,rows,bytes,sha256}]` with `year=YYYY/month=MM/{bars,coverage}.parquet` paths. Fixture
Parquet payloads are generated only for offline contract tests; production materialization always
uses and verifies MarketHub's original bytes.

Connected tests remain fail-closed and are run separately with `pytest -m connected`. As of
2026-08-24, live MarketHub health is reachable, but the newest rapidly changing `stock_daily_1d`
dataset reports publication/read-model state `not_ready`. Consequently the live daily read and the
new workspace connected path are externally blocked. Tests are not skipped and fixtures are not
substituted; the failure is retained as operational evidence rather than claimed as a pass.

Historical connected validation from 2026-08-23 remains evidence only for its exact versions: Qlib
discovery and Nautilus formal evaluation completed with matching decision and canonical input hashes.
It does not prove readiness of the current MarketHub dataset vector.

Run the complete local gate with:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -m "not connected"
uv run pytest -m connected
uv build
git diff --check
```

The project has no mypy configuration or mypy dependency, so no type-check command is asserted.
