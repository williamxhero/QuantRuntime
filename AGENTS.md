# Working rules

- This repository is an independent Quant Runtime product. It must remain runnable without Apex
  Research or any other research control-plane package.
- Qlib is the discovery framework and NautilusTrader is the formal execution framework. Prefer
  their upstream public capabilities over local replacements.
- MarketHub is the only production market-data source. Freeze both published data versions and
  fail closed on version drift, incomplete delivery, invalid coverage, ordering, or duplicates.
- Keep one canonical MarketHub dataset and one strategy/decision hash contract shared by both
  runtime paths. Do not create framework-specific copies of those contracts.
- Formal strategies must calculate signals from bars observed by the Nautilus runtime. Never feed
  Qlib target positions or future decision rows into the formal engine.
- Runtime output may contain evidence and hashes but must never persist raw market bars or a
  reusable local data cache.
- Keep A-share policy in public Nautilus seams: instruments, strategy guards, and fee model. Do not
  fork or patch Nautilus core.
- Preserve Qlib native evaluation exports and Nautilus native lifecycle, fills, account, position,
  statistics, and reports.
- Run `uv run ruff format --check .`, `uv run ruff check .`, and `uv run pytest` before committing.
- Connected tests must report MarketHub blockers honestly; never substitute fixtures for live data.
- Do not commit `runtime/` or build output. Review the exact staged file list before committing.
