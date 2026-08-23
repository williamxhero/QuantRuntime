# Validation evidence and boundary

The predecessor PoC froze the following S acceptance evidence on NautilusTrader 1.231.0:

- MarketHub data version `mhf-v1-7f2e467c54127add202f7cdb3b0d9b5dbad0d32a85286aa3687ca704b3eb82e7`.
- January 2025, `SH.600000` and `SZ.000001`.
- 18 trading days, 36 bars, 15 decisions, 6 fills.
- Total fees 31.54 CNY and final account 999,938.46 CNY.
- Three repeats produced the same normalized output hash.

The repository keeps a compact golden expectation, not a market-data copy. Engine tests use an
explicitly synthetic seam fixture whose execution prices reproduce the six frozen fills; this
proves adapter and China-rule behavior but is not represented as connected MarketHub evidence.

On 2026-08-23 the daily-window endpoint initially returned HTTP 503 `READ_MODEL_NOT_READY`, then
recovered. After recovery, the real CLI completed three connected S runs against data version
`mhf-v1-5bba83b2da9ff2780028cdb7049c44793b26d71cd78dd7eb6fcd9c010375f4ad`:

- canonical input hash: `2ef019312c35d8f03e4674b302988bb80c7a540d4b6ea4af517bdbf5ae75797b`;
- normalized output hash: `abcc9873a59bd7f9512caa30500ff5d2d02e61e4840a7b56a2bc8db569f7e36d`;
- all three runs retained 15 decisions, 6 fills, 31.54 CNY fees, and 999,938.46 CNY final account;
- engine time was 0.0267-0.0293 seconds and post-run RSS was 221.7-221.9 MB.

Nautilus native order/fill/position CSVs contain upstream-generated event and snapshot UUIDs, so
their byte hashes differ between otherwise identical runs. The reports are deliberately preserved
without rewriting; deterministic acceptance uses the canonical input and normalized semantic
output hashes. Native account, statistics, and normalized artifact bytes were stable. M/L
benchmarks were not run.

## Cross-sectional momentum contract

The neutral strategy object is:

```json
{"parameters":{"lookback_days":3,"top_k":1},"spec_revision":"1","strategy_id":"cross-sectional-momentum-topk"}
```

Its canonical strategy hash is
`f06669db3f35dd2096456df51fb69707dea3fd50d53d828bdaff8e7833bccd6d`.
The offline engine golden compares runtime decisions with a separately computed oracle, while an
anti-future-leak test removes access to that oracle from the formal engine path.

On 2026-08-23 three real CLI runs over the same January 2025 two-stock window produced:

- 15 runtime decisions from 2025-01-07 through 2025-01-27;
- cross-framework decision hash
  `be36c06594f471c74dd67784b918ce14a18e235706e441b5d210ce6bbcfaaff8`;
- canonical input hash
  `bdcc2e4f5a3c537dde1868a64ff8915f01a75648daa40860178e8270a8de00d1`;
- normalized output hash
  `ceec17c49de7610bc743f1583a33185884881256c3392a8a1e014f7eef038263`;
- five Nautilus-native orders and five fills, all filled, plus one final-day
  `no_next_session` strategy guard;
- engine time 0.0222-0.0244 seconds and post-run RSS 221.9-222.2 MB.

All identity hashes, decision rows, order/fill counts, and normalized semantic outputs were stable
across the three runs. Upstream-generated native report UUIDs remain outside deterministic byte
identity, as described above.
