# Working rules

- This repository is the Quant Runtime execution plane. Strategy Workspace is the control plane and
  owns package registration, request validation, SQLite metadata, artifacts, attempts, records, and
  canonical result schemas.
- Depend only on the public `strategy_workspace.WorkspaceClient` and `WorkspaceWorker` methods.
  Never copy Workspace schemas, SQLite repositories, artifact stores, or package administration into
  this repository.
- Qlib is the only production discovery adapter and NautilusTrader is the only production formal
  adapter. Do not register placeholder LEAN, ApexTrade, or fallback adapters.
- MarketHub is the only production market-data source. Freeze published versions and fail closed on
  version drift, incomplete delivery, invalid coverage, ordering, duplicates, or artifact mismatch.
- Formal strategies calculate signals from bars observed by NautilusTrader. Qlib outputs are optional
  discovery evidence and must never be injected as future decisions into a formal run.
- A reference snapshot reads verified bars in memory and defaults to no raw-bar persistence.
  Materialized snapshots must arrive through Workspace ArtifactRefs and be verified before use.
- `request_id`/Workspace `run_id` is the canonical idempotency identity. Failed runs require an
  explicit Workspace retry, which creates a new attempt without changing the request.
- Keep strategy implementations in registered Strategy Packages. Test-only packages may live under
  `tests/fixtures`, but this repository must not own a canonical research strategy.
- Run `uv run ruff format --check .`, `uv run ruff check .`, `uv run pytest -m "not connected"`,
  `uv build`, and `git diff --check` before committing. Run connected tests separately and report
  MarketHub blockers honestly.
- Do not commit runtime state, Workspace data, `.runtime/`, `runtime/`, `dist/`, or build output.
