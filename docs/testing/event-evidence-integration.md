# Event evidence integration verification

The executable suite is `tests/integration/test_event_end_to_end.py`.

## Coverage

- Authenticated HTTP candidate listing, confirmation, rejection, candidate editing,
  replay of the same confirmation key, conflicting reuse, stale request/version/hash/
  expiry/ETag, CSRF, and owner isolation.
- HTTP confirmation writes exactly one decision and outbox event; replay does not
  publish twice. Before indexing there are no serving event chunks.
- Real outbox dispatch, job claims/leases, `KnowledgeIndexJobExecutor`, FTS5 rebuild,
  sqlite-vec generation creation/activation/search, final authorization, citation
  sealing, replay validation, and `/v1/citations/context`.
- Citation HTTP replay includes the event version, original history/conversation,
  evidence excerpt and excerpt hash. Forged quote hashes are rejected.
- A second thread commits a new event version, soft deletion or source-history
  deletion while the worker is inside embedding. Each test verifies the worker
  fails durably, does not activate a vector generation, and exposes no stale
  event through the serving view or lexical retrieval.
- After a successful index, the same three source changes invalidate old lexical,
  vector authorization and citation results, including HTTP citation replay.

## Test boundaries

Tests use migrated temporary SQLite databases, real FTS5 and the actual sqlite-vec
adapter. Only the embedding model is replaced with a deterministic three-dimensional
vector so tests neither download model weights nor call an external model. These
are indexing/authorization tests, not embedding relevance benchmarks.

Candidate edits go through HTTP PATCH. The API currently only edits candidates;
formal-version replacement and soft deletion in the race tests are explicit
storage mutations on an independent connection/thread. History erasure executes
SQL DELETE and exercises the actual cascade trigger. These tests do not claim
coverage of a nonexistent formal-event edit/delete HTTP endpoint or the complete
privacy-journal erase workflow.

The earlier database-lock diagnosis was incorrect: two *dependency functions*
opened separate transactions inside one HTTP request. Event writes now share
`api.memory.get_db_session` with authentication. Multiple engines themselves were
not the cause.

## Reproduce

```sh
uv run pytest tests/integration/test_event_end_to_end.py
```
