# G006 evaluation preparation and independent evidence

Issue #31 optimizes only synthetic evaluation setup. Each candidate and stage
still executes every registered fixed case and generates its own observations,
execution record, signed trajectories and publication evidence. Content-identical
artifacts can share a digest; that is not permission to share execution results.

The process-local preparation cache contains only a freshly migrated database
(including migration-owned seed rows) and an initialized **empty** encrypted restic
repository. Each caller receives a private writable copy. No snapshot, candidate,
user data, approval, evaluation outcome or restored bundle is cached. Every
recovery probe continues to perform its own real backup, erase and restore.
Existing production backup encryption parameters and release gates are unchanged.

Preparation templates are validated before reuse, invalidated when their inputs
change, and cleaned when the process exits. Existing destinations are refused.
Database copies, object stores, repository copies, snapshots, erase journals and
restore targets belong to individual cases. The canary probe explicitly closes
its SQLite connection when the probe finishes.

## Verification

The baseline is commit `36fa91405dbd8800bc0160f616a0904a31e3f541`:
817 passed, no skips, pytest exit 0, clean before/after, runner wall clock
1093.426 seconds. The budget is 900 seconds using the **runner wall clock**, not
pytest's shorter internal elapsed time.

A fresh baseline profile of the two-candidate lifecycle test recorded:

| Operation | Count | Seconds |
| --- | ---: | ---: |
| Migration | 95 | 16.958 |
| restic init | 8 | 23.132 |
| Real backup | 8 | 6.618 |
| Real restore | 8 | 8.474 |
| Complete fixed suite | 8 | 56.517 |

The optimized working-tree cold-process profile kept all eight complete suites,
backups and restores: two migrations (outer test database plus cached template),
one restic initialization, and 24.018 seconds total. This development measurement
is not the final clean-checkout acceptance result.

These times overlap: suite duration includes preparation and recovery. Do not
sum all rows. Baseline profiling used Python 3.12 on Apple M4/macOS with real
restic. Each comparison starts a fresh Python process with a cold process-local
cache; reuse within that process is the optimization being measured.

Regression coverage includes the preparation helper's cold/reused templates,
corruption and input invalidation, isolated writes and preparation races, plus
real execution-service tests for independent candidates/stages and rejected
foreign evidence. Existing migration/upgrade, release-integrity, canary,
worker-recovery and real-restic tests remain in the full suite.

Final acceptance must run the delivery acceptance command from a clean checkout
of the implementation commit with an external persistent output directory and a
900-second pytest budget. Retain the report, full pytest log, JUnit, incremental
per-test outcomes, browser screenshots and API/Worker/migration logs. Report
functional correctness and runtime budget separately; this document alone does
not assert that a future SHA has passed.
