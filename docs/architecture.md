# Architecture

```text
MarketHub HTTP
  -> fail-closed canonical dataset (one shared contract)
     -> discovery/ Qlib signals, IC, risk, candidate manifest
     -> formal/ Nautilus on_bar strategy, native execution, formal manifest
        -> semantics/ golden comparison
```

`contracts/` owns canonical JSON/hash, the shared strategy specification, artifact integrity, and
the two public manifests. `markethub/` owns the sole production HTTP client and canonical daily
schema. Neither framework defines a second data lineage.

`discovery/` converts the canonical dataset to an in-memory Qlib DataFrame, uses upstream Qlib
Rank IC and risk analysis, and emits pre-matching decisions. `formal/` converts that same canonical
schema into Nautilus instruments, quotes, and bars. The formal momentum strategy computes scores
only from closes observed through `on_bar`; candidate decisions never enter its constructor.
Formal execution models CNY tick/lot rules, T+1, suspension, listing lifecycle, price bands,
minimum commission, sell stamp duty, per-fill rounding, and configurable bid/ask slippage through
public Nautilus data, strategy-guard, and fee-model seams.

`semantics/` hashes and compares the neutral decision envelope. A formal run is semantically
matched only when data version, strategy specification, canonical input, and decision hash agree.
Nautilus remains the authority for orders, fills, positions, account, and statistics.

Manifest artifacts are content-addressed with `relative_path`, `sha256`, and `content_bytes`.
Config identity is the SHA-256 of exact input file bytes. MarketHub raw bars are never persisted.
