# Authority and Lifecycle

This document defines which source is authoritative for each data type and how data moves from raw evidence to confirmed serving state.

## Authority Matrix

| Data type | Authority | Notes |
|---|---|---|
| Original PDF/web/image/Markdown upload bytes | Immutable evidence object | Stored before SQLite pointer commit; never replaced by summaries |
| Extracted text and spans | Content version rows plus immutable extracted artifact | Versioned; can be regenerated with a new processor version |
| Current visible knowledge object | SQLite current formal view | Final serving authorization always reads SQLite |
| User explicit memory | SQLite formal memory rows | Can become formal immediately if created by the user and passes validation |
| Agent-inferred user memory | SQLite candidate rows | Cannot affect formal context until user confirmation |
| External discovered knowledge | Candidate knowledge rows | Requires confirmation before formal ingestion |
| Human-readable note body | Markdown file plus SQLite pointer/version | File owns body; SQLite owns lifecycle and current pointer |
| FTS/vector/summary/cache | Derived indexes | Rebuildable; no independent visibility authority |
| Strategy release | SQLite release state plus immutable release input artifact | Only stable release can serve default traffic |
| Privacy erase progress | SQLite write-ahead erase ledger | Ledger is written before authoritative deletion |

## Candidate to Formal Lifecycle

```text
draft
-> candidate
-> evidence_ready
-> validated
-> awaiting_user_confirmation
-> confirmed
-> formal_current
```

Failure and retirement states:

```text
rejected
superseded
soft_deleted
erase_pending
erased
```

Rules:

- Candidate rows use a separate namespace from formal rows.
- Candidate retrieval may be shown in the confirmation center, but it cannot be used as official context for answers, recommendations or summaries.
- Confirmation creates a new formal generation. Derived indexes must include the formal `confirmation_generation`.
- Rejection is retained for audit and deduplication; it does not delete the original evidence.

## Processing States

Every ingestible object and derivative generation uses explicit processing state:

```text
created
-> object_stored
-> metadata_recorded
-> queued
-> processing
-> processed
-> indexed
-> ready
```

Error states:

```text
retry_wait
dead_letter
cancelled
blocked_by_privacy
blocked_by_confirmation
```

Worker jobs must be idempotent by `idempotency_key`. Lease expiry, heartbeat timeout and retry count determine recovery. A job may be retried only if its handler can safely observe prior partial work and continue.

## Immutable Objects and Outbox

Cross-storage work follows this sequence:

1. Write immutable bytes or artifact to object/Markdown storage.
2. Compute digest and size.
3. Open a short SQLite transaction.
4. Insert or update lifecycle rows and current pointers.
5. Insert an `outbox_events` row for downstream indexing, summarization or evaluation.
6. Commit the transaction.
7. Worker claims and processes outbox events idempotently.

If step 4 or 5 fails after object storage succeeds, the object is an orphan candidate. Orphan GC may delete it only when no SQLite row references its digest/path and the object is older than the configured retention window.

## Soft Delete and Privacy Erase

Soft delete is recoverable:

- Mark the logical object `soft_deleted`.
- Remove it from current formal views.
- Queue derivative index tombstones.
- Keep authoritative evidence and versions for restore until retention policy permits purge.

Privacy erase is irreversible:

1. Write an erase request and affected object set to the write-ahead erase ledger.
2. Transition affected formal/candidate rows to `erase_pending`.
3. Delete or cryptographically shred authoritative evidence bytes and Markdown bodies.
4. Remove or tombstone SQLite rows according to schema policy while preserving minimal audit proof.
5. Rebuild or purge FTS, vector, summary and cache records by source/version/generation.
6. Mark ledger entries complete only after all affected authorities and derivatives are cleared.

Erase replay is mandatory during restore from backup. A restored backup must replay completed and pending erase ledger entries before serving traffic, so old backups cannot revive erased content.

MVP does not physically deduplicate erasable evidence across logical objects. This keeps per-object erasure provable.

## Evolution Lifecycle

Runtime feedback becomes learning signal only when it has evidence references and `learning_eligible=true`. The first MVP evolution loop is retrieval and answer strategy improvement:

```text
evaluated_trajectory
-> proposal_candidate
-> evidence_ready
-> validating
-> approved
-> canary
-> stable
```

Exception states are `rejected`, `rolled_back` and `deprecated`. Only `stable` can serve default requests. `canary` can serve only explicitly controlled traffic. All other states are non-serving.

## Five Role Capability Separation

| Role | Capability | Prohibited actions |
|---|---|---|
| Proposer | Read eligible trajectories and propose minimal declarative changes | Approve, publish or modify trusted roots |
| Validator | Run fixed/dynamic eval sets and deterministic safety checks | Change eval thresholds or approve own results |
| Reviewer | Independently inspect evidence, diff and validation reports | Rely only on Proposer summaries or publish directly |
| User approver | Confirm high-risk changes, user-profile inferences and external knowledge ingestion | Bypass validation for serving release |
| Publisher | Bind approved release inputs and promote/canary/rollback | Alter proposal content, trusted roots or review evidence |

## Immutable Release Input

Every strategy release binds these eight immutable fields:

| Field | Meaning |
|---|---|
| `release_input_id` | Stable digest-addressed release input identifier |
| `target_component` | Retrieval, answer routing, prompt template ID or other allowed component |
| `proposal_id` | Source proposal and minimal diff |
| `source_trajectory_ids` | Evaluated tasks that produced the proposal |
| `validation_report_id` | Fixed and dynamic eval result bundle |
| `review_report_id` | independent Reviewer decision and rationale |
| `risk_policy_snapshot_id` | immutable snapshot of thresholds and protected sets |
| `rollback_target_release_id` | last known safe release for immediate rollback |

Release inputs are append-only. A release must be rebuilt as a new input if any field changes.
