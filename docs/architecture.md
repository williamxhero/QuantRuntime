# Architecture

The production path has four explicit stages:

```text
MarketHub HTTP -> fail-closed canonical dataset -> in-memory Nautilus data -> native evidence
```

`markethub.py` freezes `data_version`, pages the catalog and daily query, verifies the trading
calendar and response contract, then verifies the version again. `canonical.py` maps SH/SZ/BJ
symbols and hashes the exact semantic input without first materializing a large JSON document.

`engine.py` creates native equities, daily bars, and zero-spread open/close quote ticks in memory.
Bar execution stays disabled. A market order is submitted one microsecond after the next open
quote, so Nautilus performs normal order lifecycle and matching at the intended open price.

`momentum_strategy.py` is the formal strategy path. Each `on_bar` call adds only that observed
close to per-instrument history. Once all instruments seen for the session have arrived, the
strategy calculates and ranks momentum, records canonical runtime decisions, and schedules the
resulting rebalance for the next open. A close-time alert handles incomplete daily coverage and
`on_stop` finalizes the last signal day; neither accepts future decisions. The builder in
`momentum.py` is an offline oracle for golden and cross-framework comparison only and is not wired
into `run_engine`.

`china_rules.py` supplies only gaps in upstream behavior: A-share guards and a public `FeeModel`.
It does not modify engine core or recalculate the native account after execution.

`manifest.py` hashes every persisted evidence artifact. Runtime metrics are reported but excluded
from the normalized semantic output hash so clock and process-memory noise cannot break replay
identity. No raw bar artifact is written.

The manifest deliberately separates identities: `config_hash` covers the original config file
bytes, `strategy_spec_hash` covers canonical framework-neutral strategy semantics, and
`metrics.reference_decision_hash` covers the runtime `canonical-strategy-decisions.v1` envelope.
