# Coordinator replay fixtures

These four fixtures come from the real pilot-3 arm-4 runs N01-r1, N01-r2, N03-r1 and N04-r2. Extraction reads run exports and the lab's content-addressed artifact blobs. It checks each blob's SHA-256 and scans credential-shaped values before writing a fixture. Source artifacts remain immutable.

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.extract_replay \
  '/Volumes/Mrigesh SSD/eidos-v1/pilot-3/runs/N01-r1/arm4' \
  study/evals/fixtures/N01-r1.json
```

Use `--lab-home PATH` when the read-only artifact store has moved. The default comes from `run_record.json`.

## Schema `eidos-coordinator-replay/1`

- `source` identifies the block, card, arm, frozen Eidos commit and original transcript SHA-256.
- `response_artifacts` follows `artifact.registered` ledger order for every `model-response/1` blob, including artifacts absent from the adapter's accepted-decision export. Each entry includes the artifact ID, original byte SHA-256, name, ledger sequence, turn ID, linked decision ID and acceptance where exported, associated usage receipt, and the complete decoded `messages` list. A null acceptance means the exported decision has no artifact link. Rejected proposals are preserved in `turns[].decisions`.
- `requests` enumerates every response message across those artifacts in order. `artifact_id` and `message_index` locate the original message, including text, reasoning, finish reason and provider usage. It records provider response ID, start/end Unix timestamps, elapsed seconds, investigation time remaining and decision-boundary time remaining at request start. Timings use the preceding request message and following response message timestamps. A multi-request artifact has one entry per actual reply. The associated aggregate `model.usage` receipt retains transport failures and decision-level elapsed time separately.
- `timing` records the original investigation deadline, seconds as the unit, scale 1.0 and the timing derivation. A replay consumer can scale elapsed and remaining durations together for fast deterministic tests.
- `turns` preserves the ordered turn specs, decisions, result, status, timestamps and stop reason. Specs include limits, completion boundaries and exploration state.
- `needs_input` preserves the terminal input marker, and `needs_input_events` retains its ledger evidence even when an operator stop subsequently clears it.
- `receipts` retains usage, accepted-decision, turn completion, incumbent, note and investigation terminal events.
- `final_state` is the complete exported `investigation_latest.json`. `run_record` preserves adapter outcome and accounting.

The coordinator replay test belongs in Eidos core's test suite. It loads the JSON fixture, feeds response messages in `requests` order to the scripted fake provider, scales recorded durations to its fake clock and supplies the recorded receipts/state through its test harness. It asserts the corrected conclusion or explicit reasoned abstention and the relevant invariant on the build under test. The fixture captures the historical failure for that assertion. Core replay tests should verify fixture schema versions and consume artifact/message references directly.

The portable study extractor test reconstructs an arm-4 export and content-addressed blob store from N01-r1's real fixture, verifies original blob hashes, then exercises the extractor CLI. This keeps the study tests runnable independently of local pilot directories.
