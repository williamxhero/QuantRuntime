# Working rules

- This repository is a neutral integration around upstream NautilusTrader.
- Use `nautilus_trader==1.231.0`; do not fork or patch engine core.
- Market data comes only from the configured MarketHub HTTP endpoint. Never add a local,
  cached, file, database, or alternate-provider fallback to production code.
- MarketHub responses fail closed on health, version drift, completeness, cursor, ordering,
  duplicate, catalog, calendar, and canonical-data violations.
- Runtime output may contain engine evidence and hashes, but must never persist raw market bars.
- Keep A-share policy in public Nautilus seams: instruments, strategy guards, and fee model.
- Prefer Nautilus native lifecycle, ledger, positions, account, statistics, and reports over
  reimplementing equivalent functionality.
- Run `uv run ruff check .` and `uv run pytest -m "not connected"` before committing.
- Connected tests may be blocked by MarketHub readiness. Report that state; never hide it with
  fixture data or claim connected verification passed.
- Do not commit `runtime/` output.
