# Issue #29: fixed synthetic conclusion extraction evaluation

Issue #29 is evaluated with the fixed fixture at
`tests/fixtures/conclusions/issue29_synthetic_dialogues.json`. It contains 50
synthetic dialogues: 45 labelled conclusion and premise examples plus five
negative-context examples covering quotations, jokes, hypothetical statements,
explicit do-not-record language, and reported speech.

The evaluator in
`src/zhiheng/evaluation/issue29_conclusion_extraction.py` invokes
`HeuristicConversationConclusionExtractor` for every dialogue. It reports
per-case matches and applies the ADR 0006 gates:

- conclusion recall is at least 90%;
- premise recall is at least 95%;
- every matched claim has an exact source offset (100%);
- negative-context dialogues produce zero extractions.

Run the focused acceptance test with:

```bash
uv run pytest tests/evaluation/test_issue29_conclusion_extraction.py
```

The fixture contains synthetic text only. The quality score comes from fresh
extractor output, not precomputed extraction results.
