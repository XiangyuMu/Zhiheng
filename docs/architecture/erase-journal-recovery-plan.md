# Erase journal crash recovery — implementation plan

Status: proposed; not implemented or accepted as completion evidence.

## Observed gap

`ExternalEraseJournal.append_intent` fsyncs the journal, then replaces its signed head.
`test_erase_journal_crash_boundary.py` demonstrates that a crash between these operations
leaves a durable intent that subsequent reads and appends reject. This is fail-closed but does
not meet the brief's requirement to resume authenticated erase intents after crashes.

The API startup and Worker currently do not call `replay_external_journal` or `replay_pending`.
Those calls exist in restore/replay utilities. Repairing the journal alone therefore cannot
prove that a restarted serving process will apply an intent written before the database commit.

## Proposed durable witness

Before appending an intent, write and fsync an authenticated pending sidecar containing the
exact signed next record, previous signed head (or genesis), old journal byte length and byte
digest, and expected signed new head. The witness signature must cover every field; a head
digest alone does not bind the filesystem append boundary. Atomically publish the sidecar and
fsync its directory. Only then append/fsync the journal, replace/fsync the head, and remove the
pending sidecar with a final directory fsync. Pending metadata must contain only existing
journal identifiers/digests, never personal content, erase reasons or credentials.

Recovery must verify the witness, prior anchor, existing complete chain and exact expected
suffix before making any change. It must never skip malformed records, truncate history,
re-sign history or infer an erase target. A partial final write may only be completed by
appending the missing bytes of the authenticated expected record after an exact prefix match.
A matching already-published head permits cleanup of a stale witness without re-appending.

Legacy journal/head pairs remain readable unchanged. Missing or inconsistent legacy heads
without a valid pending witness remain fail-closed. Local signatures do not prove freshness
against coordinated rollback of every journal/anchor copy; independent latest-copy retention
is still a separate deployment requirement.

## Locking and serving boundary

- Preserve the stable journal inode used by current flock users; do not replace the journal
  itself as part of repair.
- Exclusive append/recovery operations must use private already-locked readers; public strict
  readers must not recursively acquire another exclusive lock.
- Restore currently holds a shared journal lock around installation. Do not attempt exclusive
  recovery from `load()` while that lock is held. Recover before the shared-lock interval, then
  strictly revalidate inside it; a new interrupted writer in between must fail closed.
- Introduce an explicit startup recovery barrier before API readiness and before Worker
  indexing/dispatch. Verify/recover the external journal outside a database transaction, then
  replay authoritative intents and complete pending physical erases. A failed recovery must
  prevent serving, not merely log a warning.
- Preserve the database maintenance-lock ordering used by restore. Evaluate startup races
  between API and Worker; do not add a lock order that deadlocks a live erase request.
- Distinguish a truly fresh empty deployment from a missing required journal using durable
  database state; a missing file must not silently reset an established erase history.

## Required verification

1. Crash at every fsync/publication boundary, for both genesis and non-empty journals.
2. Exact-prefix partial append recovery, malformed suffix rejection and unchanged bytes on
   rejection; wrong key, altered witness, altered head, altered prefix and duplicate request IDs.
3. Recovery repeated after its own interruption; no duplicated record or advanced sequence.
4. Concurrent reader/writer/recovery and restore shared-lock ordering, using synchronization
   events rather than timing assumptions.
5. A real API/Worker restart with a durable external intent but no committed database intent:
   affected knowledge/memory cannot be served or indexed before erase replay completes.
6. Old encrypted-backup restore with newer erase intents, repeated replay and physical-byte
   verification; damaged/missing required witnesses fail without replacing the live database.

Independent architecture review is required before relying on the proposed protocol. Current
Worker/proposal-evaluation approvals do not cover this separate privacy recovery design.

Read-only protocol review confirms the need for the byte-length/head bindings and exclusive
recovery lock. Its conservative recovery set covers no suffix or the complete expected suffix.
The exact-prefix partial-write extension above needs its own adversarial review and tests before
implementation; it must not be assumed safe merely because a JSON prefix looks plausible.
