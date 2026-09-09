# Worker Runtime

Zhiheng keeps one API process and one background worker process in the MVP deployment. The
worker is a native long-running Python entry point exposed as `zhiheng-worker`; Docker Compose
execs it directly so container restart policy can observe real process failures.

## Modes

- `zhiheng-worker` runs continuously until `SIGTERM` or `SIGINT`.
- `zhiheng-worker --once` processes one bounded batch and exits.
- `--role worker` runs outbox, knowledge indexing, candidate-only maintenance, and proposal
  evaluation with an injected Validator context. It does not claim publication jobs.
- `--role publisher` also runs publication-capable jobs with an injected Publisher context.
  Validator and Publisher command contexts remain separate even in this single process.

The only valid roles are `worker` and `publisher`. Unknown CLI roles fail through argparse, and
unknown `ZHIHENG_WORKER_ROLE` values fail during startup.

## Exit And Retry Contract

`run_once(settings, knowledge_executor_factory=...)` remains a test-friendly convenience function
and returns the number of completed units of work. That count is not a process exit code.

The CLI maps outcomes explicitly:

- `--once` success exits `0`, even when work was completed.
- Startup configuration or unknown environment role exits nonzero.
- Database failure in `--once` exits nonzero.
- Continuous mode idles through an event wait when no work is available.
- Continuous mode backs off through an event wait after transient database failures.

The signal handlers set a shared stop event. Idle and backoff sleeps wait on that event, so shutdown
does not wait for a busy loop or a fixed shell sleep to finish.

## Privacy Boundary

Database errors are logged by class and role only. SQL statements, bound values, exception payloads,
and personal content are not written to worker logs.

## Proposal Evaluation

`POST /v1/evolution/proposals/{proposal_id}/evaluation-requests` queues an outbox event after
authentication, CSRF, ETag and idempotency checks. The body permits a reason only, not caller
scores, execution IDs or capabilities. The event aggregate ID determines the job's proposal ID.

The worker executes the frozen proposal through `ProposalExecutionService`, then validates its
protected execution record. Successful validation ends at `validating`, not `approved` or
`stable`; review, user approval and publication retain their separate gates. Retrying after a
validation commit but before job acknowledgement reuses the execution and validation records.

## Job Acknowledgement

Queue claiming is limited by the executor's injected command contexts, not by payload role
claims. Reclaiming an expired lease closes the prior unfinished attempt as `LeaseExpired`.
Acknowledgement checks job ID, attempt count, lease owner and the exact still-processing attempt
belonging to that job in the database write condition. Swapped, missing, finished or superseded
attempts cannot acknowledge another job or modify its attempt history.

Both evolution and knowledge-index queues enforce this fence. An expired final attempt cannot
remain `processing` forever: a bounded scan limited to the worker's allowed queue marks it dead
and records one dead letter. Its effects may already have committed; the record requests
reconciliation rather than asserting that no effects occurred. Knowledge-index activation also
rechecks the current job lease before publishing a derived vector generation.

Candidate-only maintenance binds each job's idempotency key to a request digest. A database write
serializes competing executions before checking the receipt. Draft outputs and their receipt
commit together; replay reads the existing outputs without generating additional drafts. The
append-only lock and receipt tables contain digests and output references only, not the original
evidence, failure description or reason text. Those values remain in the original lifecycle
records, so the receipt does not create another copy of personal content.
