# Orphan object GC — implementation plan

Status: not implemented. Based on the independent architecture review of the current
object-store/ingestion paths; the existing enumerator must not be connected directly to unlink.

## Safety problem

Object files are published before SQLite references are committed. Neither an age threshold
nor a last-minute reference lookup protects a writer stalled between these two operations.
The API `started` receipt does not identify the files and is absent from direct service calls.

## Required protocol

1. Reserve a UUID group and its three canonical URIs in a short SQLite transaction before
   publishing files. Persist a writer token, renewable lease and lifecycle state in a dedicated
   preparation table; do not create formal evidence rows at reservation time.
2. Carry this preparation identity through every API and direct-service prepare/commit path.
   In one short transaction, CAS the active preparation to committed and create immutable
   evidence/version references, current pointers and outbox rows. A stale/reaped token must
   never authorize a late commit.
3. Discover only regular, non-symlink files matching the known UUID group layouts. Ignore
   arbitrary files, control files and temporary names. Require a positive retention window
   and bound each maintenance batch. Protect references from all lifecycle states, including
   candidates, old versions and soft-deleted objects.
4. Under a serialized GC command, atomically claim expired preparations for reaping. For legacy
   groups, insert a tombstone before deletion so a later writer cannot adopt the group. Recheck
   all three authoritative URI columns and unresolved privacy-erase ownership in that same
   transaction. A referenced or ambiguous group is not a GC target.
5. Perform filesystem work after the transaction. Recheck canonical root, exact filename,
   regular-file identity and retention before each unlink; fsync affected directories. Treat
   missing files as idempotent success. Preserve retry state after partial failure.
6. Make stale writer/publication races explicit: a writer that loses its lease must not commit
   references, and files published after a reap must remain discoverable for a later GC pass.
   Do not make `reaped` a permanent exemption from discovery. A per-group filesystem lock is
   an alternative only if it is consistently ordered with database locks and used by all
   publication/deletion paths.
7. Add an explicit low-frequency Worker/admin maintenance caller and observable counts. Do
   not run GC synchronously in user ingestion requests or use privacy erase as a GC substitute.

## Proof required before enabling deletion

- Writer delayed past retention, with GC before/after final publication and before DB commit.
- Crash after any file publication; stale token commit rejection; repeated reaping after a
  delayed writer publishes additional unreferenced bytes.
- New references between initial discovery and final CAS; every production ingestion path
  covered, with no token-free fallback.
- Candidate, soft-deleted and historical references preserved; pending physical privacy erase
  retains separate ownership.
- Symlink, malformed name, unrelated file and temporary-file exclusion; root containment.
- Partial unlink failure, missing-file retry, two concurrent GC workers and bounded batches.
- No file reading/embedding/parsing or large I/O inside a SQLite transaction.

Only synthetic temporary stores may be used for implementation tests. Existing real files must
not be deleted as a diagnostic step. This plan is not proof that orphan GC is delivered.
