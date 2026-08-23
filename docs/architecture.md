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

`china_rules.py` supplies only gaps in upstream behavior: A-share guards and a public `FeeModel`.
It does not modify engine core or recalculate the native account after execution.

`manifest.py` hashes every persisted evidence artifact. Runtime metrics are reported but excluded
from the normalized semantic output hash so clock and process-memory noise cannot break replay
identity. No raw bar artifact is written.
