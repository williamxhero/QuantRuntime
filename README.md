# MarketHub Nautilus

Independent, neutral A-share backtest runner which feeds MarketHub daily data directly into
upstream NautilusTrader 1.231.0. It owns only the integration and China-market policy gaps;
Nautilus owns the event loop, order lifecycle, matching, fills, portfolio, account, positions,
statistics, and native reports.

## Guarantees

- Python 3.12 and a pinned upstream NautilusTrader wheel.
- MarketHub is the only production market-data source.
- Data is fetched into memory and injected with `BacktestEngine.add_data`; raw bars are never
  written to the output directory.
- Health/version, response completeness, cursor, ordering, duplicate, and canonical-schema
  checks fail closed.
- SH/SZ/BJ instruments use CNY, 0.01 price ticks, and 100-share lots.
- Next-open execution, T+1, suspension/no-bar, listing lifecycle, price-band, minimum commission,
  sell stamp duty, and per-fill cent rounding are implemented through public Nautilus seams.
- The output keeps Nautilus native orders, fills, positions, account and statistics plus a
  content-addressed run manifest.

## Setup and run

```powershell
uv sync --python 3.12 --extra dev
uv run python -m markethub_nautilus.cli run `
  --config configs/s-validation.json `
  --output runtime/s
```

The last stdout line is one compact JSON object:

```json
{"status":"success","run_id":"...","manifest_path":".../run_manifest.json"}
```

The validation config is the frozen S rule-seam scenario. Its `rule_overrides` are explicit test
conditions, not claims about historical MarketHub state. Production configs must omit them and
leave `allow_rule_overrides` false.

## Verification

```powershell
uv run ruff check .
uv run pytest -m "not connected"
uv run pytest -m connected
```

The connected test does not fall back when MarketHub is unavailable or its read model is not
ready. See [the evidence boundary](docs/validation.md) for the frozen S result and current live
gate status.
