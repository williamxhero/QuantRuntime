# Validation evidence and limits

On 2026-08-23 three complete live two-stock workflows ran over January 2025 MarketHub data. Every
workflow completed `discover`, `evaluate`, and `golden-check` with stable identities:

- candidate run: `qr-discover-7babb21175b6d1e51b1bbb58`;
- formal run: `qr-formal-a9ea3b53125454629aa55040`;
- MarketHub data version:
  `mhf-v1-2a6b9abd5e6daa9374bdc8d97b4644ad3cecb1d82a597418d740c20f14a7fc3d`;
- daily dataset version:
  `mhd-v1-66f21b5a0b568b996d906d2df2e3a908f5877a7612bcd60046b1b6b19bcf6de1`;
- strategy spec hash:
  `7d291af79d6611bf9d1852c9a3b46af497a95de439cb44f3251ba0b56c2b0b91`;
- canonical input hash:
  `d9c344dffcb2393042661adf39d1c4e4e1c9804abc82f3ce0ddbd9c1c853dfb0`;
- Qlib candidate and Nautilus runtime decision hash:
  `2f49985541decc84ecf2fc894009e7d09ee832da8e9350bd865bed08283cd8f4`;
- normalized formal output hash:
  `754795064ec1960a155048428f8b5a6ba1c3de663967d7f832fa0a2b308b6ead`.

Each run produced 15 candidate/runtime decisions, five Nautilus orders, five fills, and
`metrics.semantic_match=true`. Engine time was 0.0215-0.0258 seconds and post-run RSS was about
306-307 MB.

The S workflow is a contract and integration gate, not an M/L scale benchmark. Native Nautilus
CSV files can contain upstream-generated event UUIDs, so semantic determinism is established by
the canonical identities and normalized output rather than requiring native report bytes to match.
The pinned Qlib dependency also emits an upstream Gym deprecation notice under NumPy 2; it does not
change command exit status or the final machine-readable JSON line.
