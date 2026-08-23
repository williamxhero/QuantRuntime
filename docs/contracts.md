# Public manifests

These manifests are neutral process-boundary contracts. Apex Research is one possible external
consumer, not a dependency or namespace owner; another research control plane can replace it, and
multiple consumers can independently verify the same artifacts. Selecting `evaluate --runtime`
changes the formal adapter only and does not change the manifest schema or CLI stdout shape.

## Candidate

```json
{
  "schema": "quant-runtime.candidate-manifest.v1",
  "run_id": "qr-discover-...",
  "framework": "Qlib",
  "framework_version": "0.9.7",
  "status": "passed",
  "data_version": "...",
  "dataset_version": "...",
  "config_hash": "sha256 of exact discovery config bytes",
  "strategy_spec_hash": "sha256 of canonical strategy spec",
  "canonical_input_hash": "sha256 of shared canonical MarketHub dataset",
  "artifacts": [
    {"relative_path": "...", "sha256": "...", "content_bytes": 123}
  ],
  "metrics": {
    "reference_decision_hash": "sha256 of canonical decision envelope",
    "framework_version": "0.9.7",
    "observation_count": 28,
    "signal_days": 15,
    "candidate_rows": 15,
    "mean_rank_ic": 0.0,
    "quick_gate_passed": true,
    "fetch": {}
  }
}
```

The candidate artifacts include `strategy_spec.json`, `strategy_decisions.json`, Qlib signals,
Rank IC, candidates, risk analysis, and a native-capability recorder export.

## Formal

```json
{
  "schema": "quant-runtime.formal-manifest.v1",
  "run_id": "qr-formal-...",
  "framework": "NautilusTrader",
  "framework_version": "1.231.0",
  "status": "matched",
  "data_version": "...",
  "dataset_version": "...",
  "config_hash": "sha256 of exact formal config bytes",
  "strategy_spec_hash": "same canonical strategy hash",
  "canonical_input_hash": "same canonical MarketHub dataset hash",
  "normalized_output_hash": "sha256 of normalized formal semantics",
  "candidate_run_id": "qr-discover-...",
  "candidate_manifest_hash": "sha256 of exact candidate manifest bytes",
  "artifacts": [
    {"relative_path": "...", "sha256": "...", "content_bytes": 123}
  ],
  "metrics": {
    "candidate_decision_hash": "...",
    "formal_decision_hash": "...",
    "semantic_match": true,
    "data_version_match": true,
    "dataset_version_match": true,
    "strategy_spec_match": true,
    "canonical_input_match": true,
    "decision_match": true,
    "fetch": {},
    "runtime": {}
  }
}
```

The current formal artifacts are Nautilus native orders, fills, positions, account, statistics, the
runtime decision envelope, and the normalized formal output. `semantic_match` is false if any
lineage or decision identity differs. Future formal adapters must preserve these neutral identity
fields while keeping their framework-native reports inside the adapter boundary.
