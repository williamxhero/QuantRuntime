# Architecture

```text
Research control planes (outside this repository)
  -> neutral CLI + candidate/formal manifests
     -> application/ use-case orchestration
        -> market_data/markethub/ fail-closed canonical dataset
           -> discovery/qlib/ signals, IC, risk, candidate manifest
           -> formal/ FormalRuntime seam
              -> formal/nautilus/ on_bar strategy, native execution, formal manifest
        -> semantics/ golden comparison
```

`contracts/` owns canonical JSON/hash, the shared strategy specification, artifact integrity, and
the two public manifests. `market_data/markethub/` owns the sole production HTTP client, the
MarketHub anti-corruption boundary, and the canonical daily schema. It is not another MarketHub
service. Neither framework defines a second data lineage.

`application/` owns command-independent orchestration. `cli.py` only parses arguments, dispatches
an application use case, prints the final JSON object, and returns its exit code. This keeps the
public process boundary stable while framework adapters evolve.

`discovery/qlib/` converts the canonical dataset to an in-memory Qlib DataFrame, uses upstream Qlib
Rank IC and risk analysis, and emits pre-matching decisions. `formal/interface.py` and
`formal/registry.py` define the minimal engine selection seam; `formal/nautilus/` converts the same
canonical schema into Nautilus instruments, quotes, and bars. The formal momentum strategy computes
scores only from closes observed through `on_bar`; candidate decisions never enter its constructor.
Formal execution models CNY tick/lot rules, T+1, suspension, listing lifecycle, price bands,
minimum commission, sell stamp duty, per-fill rounding, and configurable bid/ask slippage through
public Nautilus data, strategy-guard, and fee-model seams.

`semantics/` hashes and compares the neutral decision envelope. A formal run is semantically
matched only when data version, strategy specification, canonical input, and decision hash agree.
Nautilus remains the authority for orders, fills, positions, account, and statistics.

Manifest artifacts are content-addressed with `relative_path`, `sha256`, and `content_bytes`.
Config identity is the SHA-256 of exact input file bytes. MarketHub raw bars are never persisted.

Research control planes are peer repositories outside Quant Runtime. They may replace Apex Research
or run in parallel, but Quant Runtime never imports them. A future LEAN adapter belongs at
`formal/lean/`, parallel to `formal/nautilus/`; it must preserve the neutral formal manifest and let
LEAN own its native execution. The repository intentionally contains no fake LEAN implementation or
duplicate matcher, order, fill, account, or statistics subsystem.
