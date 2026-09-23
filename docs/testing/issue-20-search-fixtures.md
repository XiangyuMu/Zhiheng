# Issue #20: searchable evaluation fixtures

G005/G006 used to create current knowledge and FTS rows without a completed
`knowledge.index` job. Both lexical and vector retrieval require that completion
proof; the fixtures consequently returned no knowledge despite populated serving
views.

`evaluation/search_fixtures.py` now creates a pending synthetic indexing job,
claims it through `KnowledgeJobRepository`, rebuilds the real FTS index, and
completes the claim with an attempt record. This is a synchronous evaluation
fixture, not an acceptance test for the separately running Worker. Production
retrieval gates are unchanged. G005 builds and activates its deterministic vector
generation separately; G006 explicitly exercises FTS without an embedding model.

Regression commands:

```sh
uv run pytest tests/evaluation/test_g005_runtime_metrics.py tests/unit/test_g006_knowledge_case.py tests/unit/test_g006_memory_generation.py
uv run pytest tests/unit/test_g006_runner.py tests/unit/test_g006_runner_provenance.py tests/unit/test_g006_shadow_snapshot.py tests/integration/test_answer_memory_context.py tests/integration/test_answer_gateway_api.py tests/integration/test_decision_memory_context.py
```

Observed profiles:

| Probe | Retrieval profile | Recall | Citations/conflict |
|---|---|---|---|
| G005 | `real_fts_deterministic_fixture_vectors` | 1.0 (hybrid and vector) | Coverage 1.0; conflict surfaced |
| G006 boundary | `real_fts_vector_unavailable` | 1.0 | 2 citations; conflict detected |
| G006 migration | `real_fts_vector_unavailable` | 1.0 | 2 citations; conflict detected |

G005 reports zero unauthorized leaks and candidate activations. G006 excludes
stale chunks and validates the final manifest. Tests assert nonempty evidence;
an evidence-bearing G005 case with no citations now gets coverage 0.0.
