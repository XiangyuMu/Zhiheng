# G006/G007/G008 rework cleanup evidence

Status: in progress; this is not a final quality approval.

## Scope and behavior lock

The bounded cleanup covers release validation, evaluation parsing, knowledge
confirmation, knowledge job dispatch, erase journals, restore scripts, and their
regression tests. Before each security change, existing targeted tests were run;
new counterexamples cover malformed scores, explicit failure tags, changed
Proposer identity, duplicate erase request IDs, and concurrent journal writes.

## Cleanup plan and findings

1. Remove unbound serving entry points: deleted the public `run_agentic_rag`
   convenience wrapper; eight direct service budget tests passed.
2. Repair masking defaults at evidence boundaries: strict boolean fields and
   finite scores in `[0,1]` replace truthiness; explicit failure tags are retained.
   Seventeen evaluator tests passed after that pass.
3. Verify identity against immutable creation evidence: changing the mutable
   proposal identity cannot pass validation; thirteen release attack tests passed.
4. Preserve restore audit history: external replay no longer deletes old ledger
   rows. Journal appends are serialized and fsynced before the database intent.
5. Complete deployment dispatch and authorized knowledge ingestion, then rerun
   the full suite and independent review on the settled tree.

Fallback classification:

- Retrieval degradation with explicit reasons and final authorization is a
  grounded fail-safe path; retain its primary/degraded regression coverage.
- Job exception capture with persisted failed attempts is a grounded retry path.
- Unknown outbox events marked processed without a handler were masking failures;
  dispatch rework must keep them visible and prove supported handlers execute.
- Default erase-journal secret/path and operational restore assumptions require
  final deployment review; they are not accepted as completed security evidence.

## Verification limits

The latest journal-only concurrent/reused-request tests and scoped Ruff/mypy
passed. The wider restore suite needs rerunning after the knowledge authorization
interface migration settles. Earlier green full-suite results predate that
interface change and do not prove the current full tree.

Independent architecture status remains BLOCK until all reported invariants are
proved. Outstanding work includes full backup coverage required by the ADR,
restore serving/WAL coordination, journal truncation and recovery authority,
actual protected evaluation execution provenance, and authenticated knowledge
confirmation. Do not use this report to mark the aggregate goal complete.
