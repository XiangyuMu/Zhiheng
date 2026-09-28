# Issue #25 decision-run erasure evidence

The decision lineage matrix exposed an audit omission: an unbound legacy decision
run with an exact source ID in its JSON was preserved without an unresolved row.
The fix audits that ID without treating it as authoritative provenance. Source
bindings remain the only authority for clearing derived payloads.

## Regression coverage

- `test_decision_erase_lineage.py` exercises formal memory and knowledge sources:
  bound paraphrases are erased; unrelated bindings, identical text, short
  substrings, unbound paraphrases, exact IDs and corrupt recommendation/review
  payloads are preserved. Exact IDs, known text and corrupt legacy data are audited.
- `test_decision_erase_restore.py` creates a real restic snapshot before erasure,
  appends both source erasures to the independent journal, and restores twice.
  Bound runs/receipts (including corrupt payloads) stay erased; legacy records
  remain auditable. Formal search returns only the unrelated same-content source,
  its bytes survive, the erased source's object is absent, and analyze/save cannot
  reuse the erased run (409/404).
- `test_answer_replay_authority.py` retains the receipt regressions.

Unbound paraphrases and short substrings are preserved but cannot be attributed
or individually audited by this exact-match policy. These tests do not establish
complete semantic discovery of historical data with missing lineage.

Run the focused reproduction with:

```sh
uv run pytest -q tests/integration/test_decision_erase_lineage.py tests/integration/test_decision_erase_restore.py tests/integration/test_answer_replay_authority.py
```

For final evidence, run `scripts/delivery_acceptance.py` from a clean checkout at
the final commit, using a new output directory outside the checkout. Its report
records the full SHA, clean-before/after checks, full pytest results, browser
acceptance and extraction results. Evidence from an earlier SHA is not a substitute.
