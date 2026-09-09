# Decision context audit — not final approval

Sources: PRD §11.4 and FR-05, memory field dictionary, Ultragoal brief invariants,
`tests/fixtures/decisions/g005_decision_cases.json`.

The initial audit found that decision generation received only the problem,
not option descriptions or resolved formal goals. Template/type IDs were not
registered constraints. Model calls lacked the answer service's current-source,
memory-snapshot, transaction and budget checks. Cached analyses and saves were
not reauthorized. Saved decision references used keys ignored by MemoryRepository.

## Required evidence for the integration

- The answer and decision services share one bounded supplied-manifest consumer.
  No legacy model-only path can skip its guards.
- All option descriptions and only resolved current formal goals enter the
  canonical task prompt. Unknown, candidate and non-goal references are refused.
- L0/L1 are separate personalization context, never knowledge citations.
- Model calls run outside SQLite transactions; changed memory or knowledge
  invalidates output. Actual stop reason and complete budget usage survive
  persistence and API serialization.
- Cached analyses and initial saves validate current memory and citation proofs;
  missing legacy proof is not authorization.
- Full supplied-memory source IDs, not only model-reported refs, bind derived
  results to privacy erase. Erasure clears derived receipts/runs without deleting
  idempotency keys or replaying an external call.
- Explicit save keeps user note and actual evidence-object/span associations,
  survives restart, and respects the canonical `decision` memory type.

## Still outside a completed integration claim

Context and authorization plumbing do not prove FR-05 decision quality. The
existing benefits/costs/preference/change-condition template is incomplete.
Required option-level analysis, recommended next step, multi-round retrieval,
historical decision L2 retrieval, final user choice and outcome review require
their own behavioral tests. Original decision evidence must eventually be stored
and indexed under the immutable evidence/outbox/erase lifecycle; a decision run
or hashed prompt is not that original evidence.

This audit is a work checklist. Only fresh tests and independent review can mark
the integration complete; it does not change the original PRD or goal scope.

## Follow-up safety corrections

The scoped architecture review found uncited completed recommendations, raw
formal IDs in the outbound decision query, a save validation/commit/write race,
and missing budget stop reasons in the API schema. It also found hidden model
caveats, absent execution lineage and incorrectly bound saved-result replay.

Corrections now have targeted regression coverage:

- `test_decision_grounding.py`: missing claims are not saveable, model caveats
  and claims survive reload/save, option labels containing colons remain intact,
  retrieval run/release IDs agree, and direct domain saves reject forged/stale runs.
- `test_decision_safety_regressions.py`: budget exhaustion returns normally with
  zero model calls, invalid/duplicate goal keys fail before receipt creation,
  a competing SQLite writer is locked out during both validation and save,
  a change before lock acquisition is rejected without formal writes, unrelated
  saved-memory receipt bindings fail, and second-check citation staleness has an
  explicit 409 plus terminal failed receipt.
  Model failures count one logical invocation; deterministic clock advancement
  verifies that over-budget generation is discarded and elapsed time survives reload.
- `test_decision_gateway_payload.py`: the real API/gateway path with a synthetic
  counting transport excludes raw formal memory/version IDs and candidate values
  while round-tripping allowed citation/memory aliases.
- `test_decision_transaction_boundary.py`: the actual request Session has no
  transaction during the model callback.
- `test_decision_publication_race.py`: actual journal-backed formal-memory and
  knowledge erasure after generation's final validation cannot publish a new
  run or stale receipt; a competing writer remains locked out at receipt
  completion. These regressions required a separate final publication lock,
  not just the explicit-save lock. Three cases pass after the repair.

The old G005 runtime evaluation now consumes a real formal goal state key and
canonical memory snapshot through the bounded answerer, rather than substituting
knowledge IDs for goals. The API uses structured lookup for saved-decision
authorization; raw SQL remains in domain/repository code.

The pre-correction full run finished with 438 passed / 2 failed (API SQL boundary
and the legacy G005 evaluation). Both failures have passing targeted regressions.
The post-correction full run finished with 453 passed in 761.27 seconds, using
the real pinned restic binary. Scoped independent re-review is now CLEAR;
do not interpret either test run as global approval.
That full run started before the final publication-race repair and the journal
crash characterization tests. A later targeted run covers the publication
repair with decision, memory, gateway, erasure and evaluation tests (24 passed,
before the additional receipt-lock case); the three publication cases then pass
together. The latest combined decision/cache/restart/evaluation/journal suite
passes 53 tests. These overlap with the 453-test baseline and must not be added
to it. Ruff and mypy for all source/tests pass (174 files).

The follow-up review extended publication revalidation to **all** outcomes,
not only generated content: six real erase races now cover completed,
model-failed and input-budget exits against formal-memory and knowledge sources.
The seventh case checks receipt completion retains the write lock. An earlier
200/memory-context-changed decision expectation was tightened to 409/no run,
preventing stale identifiers from being republished. Shared model invocation
accounting and supplied-manifest elapsed-budget checks were corrected as well.
The latest expanded suite covers 100 passing tests across those decision cases,
answer gateways/context, query-service/routing units, cached-result authority,
restart persistence, G005 runtime evaluation and journal crash characterization.
Ruff and mypy for source/tests remain clean (174 files). This is the latest-code
verification; the 453-test full baseline predates the publication/budget fixes.

## Scoped independent review result

`final_architect_v3` returned **Scope CLEAR** for this decision safety integration
after independently running 30 tests across seven files, targeted Ruff, and mypy
over ten files. The review confirmed cited-claim requirements, outbound aliases,
all-outcome publication reauthorization/locking, transaction-free generation,
failure/elapsed-budget accounting, and write-locked persisted-run validation on save.
This is not a global architecture CLEAR or the final independent code-reviewer
APPROVE; the original invariant/cleanup/deployment gates remain open.
