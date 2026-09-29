# Issue #2: deterministic draft revision race

`test_same_etag_concurrent_draft_edits_have_one_winner` retains the HTTP contract:
one successful edit, one 409, stale ETag rejection, idempotent replay and retained
source/version history. Its request-start barrier alone cannot prove a stale-read
race: authentication updates `auth_sessions.last_seen_at`, which can serialize
SQLite writers before either handler reads the draft.

`test_draft_cas_rejects_writer_that_read_the_same_version` adds the repository
boundary proof. Two independent real sessions call the real `get`; a test-only
wrapper pauses each immediately after its first read. Neither can continue until
both have read version 1 and the same ETag. The CAS, version insertion and commit
are unmodified. Subsequent reads in relation suggestion bypass this first-read
barrier. Assertions require one winner, a version-conflict loser, exactly two
versions, unchanged original bytes, preserved source/premises/excerpt, and only
the winner's operation receipt.

Run both tests:

```sh
uv run pytest -q tests/integration/test_conclusion_draft_concurrency.py
```

The mutation check temporarily removed `AND current_version=:old` from the
conditional update. The new test failed with a duplicate `(entry_id, version)`
insertion rather than accepting the loser as a version conflict. Restoring the
guard passed five consecutive runs of both tests. Production code is unchanged.
This proves the test detects removal of the CAS guard, rather than only observing
successful scheduling in the HTTP layer.

Raw mutation and repeat logs are retained outside the checkout under
`/Users/muxy/Projects/Zhiheng-delivery-evidence/issue2-deterministic-cas/`.
Final full-suite evidence must identify its tested commit separately; this
focused regression does not establish #4/#13 delivery or browser acceptance.
