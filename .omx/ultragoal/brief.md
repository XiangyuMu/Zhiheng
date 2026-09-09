# Zhiheng MVP durable implementation brief

Implement and verify the single-user Zhiheng self-evolving personal knowledge Agent in this repository from the approved product and architecture sources:

- `docs/product/PRD.md`
- `docs/architecture/continuous-evolution.md`
- `docs/architecture/adr/0001-mvp-technology-stack.md`
- `docs/ROADMAP.md`

The current uncommitted Step 0 ADR, benchmark code, synthetic benchmark results, README and Roadmap edits are in-scope starting work. Preserve and verify them; do not discard or overwrite user changes. Do not push or create commits unless the user explicitly requests it.

## Required durable goals and order

1. Architecture baseline and project foundation: finish Step 0 contracts, domain taxonomy, memory field dictionary, authority/lifecycle document, SQLite data model, RAG/evolution evaluation spec, Python 3.12 project skeleton, CI and the accepted technology ADR. Target-server benchmark remains an explicit deployment-signoff task until server access exists; it does not permit invented measurements.
2. Evidence ingestion and authority: immutable evidence objects for PDF/web/Markdown/image, content versions and spans, SQLite metadata/current pointers, transactional outbox, idempotent Worker, FTS5, sqlite-vec generation rebuild, soft delete and write-ahead privacy erase foundations.
3. Privacy, authentication and model gateway: single-user server-side session, Argon2, secure cookies/CSRF, secrets isolation, fail-closed data minimization/classification/redaction/recheck, local Ollama and explicitly enabled external provider adapters, provider/audit visibility and no gateway bypass.
4. User memory and confirmation center: explicit facts versus Agent inference, candidate/formal isolation, evidence/confidence/version fields, confirm/edit/reject/batch operations, L0/L1/L2, current state, delete/restore/rollback, and deterministic proof that unconfirmed candidates cannot affect formal context or recommendations.
5. Retrieval, Q&A, decision support and knowledge-gap recommendations: structured direct lookup, Chinese FTS5 plus vector hybrid RAG, SQLite final authorization, citations and conflicts, bounded Agentic RAG, decision templates without external actions, and explainable goal-linked gap recommendations.
6. Verifiable evolution control plane: append-only trajectories, result/process/quality evaluation, Proposer/Validator/Reviewer/User approver/Publisher capability separation, immutable release input binding, protected risk policy, fixed/dynamic boundary/migration/retention/safety sets, replay/shadow/canary/stable/rollback, and one complete retrieval/answer-strategy promotion plus rollback demonstration.
7. Deployment, backup/recovery and final quality gate: Docker Compose with Caddy/API/Worker and optional Ollama, encrypted backup and erase-ledger replay, clean-environment restore, observability, hostile E2E tests, final ai-slop-cleaner, post-cleaner verification, architecture-invariant audit, independent code-reviewer approval and architect CLEAR.

## Architecture invariants

- Single-user modular monolith deployed as one API process and one background Worker process sharing domain/application code; no premature microservices.
- Immutable evidence bytes are authoritative for original content; SQLite is authoritative for lifecycle, current version, visibility, confirmation generation, processing state and strategy release state; Markdown/declared strategy artifacts are content authorities; FTS/vector/summary/cache are derived and rebuildable.
- Candidate and formal data are isolated by repository/namespace, and every retrieved source is reauthorized against the SQLite current formal view before entering a model context or user response.
- Agent-inferred user-profile changes require user confirmation before becoming effective. External knowledge also requires confirmation before formal ingestion.
- External model transmission is disabled by default and must pass one non-bypassable fail-closed privacy gateway. Unknown classification, uncertain redaction/recheck, no approved provider or unavailable required local model causes an explained refusal, never raw-data fallback.
- API and Worker use SQLite WAL and short transactions; no model, network, parsing, embedding or large-file I/O inside a transaction. Cross-storage work uses immutable-object-first commit, SQLite current pointer plus outbox, idempotent jobs, orphan GC and shadow index generations.
- Soft delete is recoverable. Privacy erase is irreversible and uses a durable write-ahead ledger before any authoritative deletion; erase resumes idempotently after crashes and old-backup restore cannot revive erased objects. Erasable evidence objects are not physically deduplicated across logical objects in MVP.
- Trusted roots cannot evolve: authentication, capabilities, Publisher, privacy/erase gates, confirmation gates, audit, validators, eval sets and release thresholds, stable backup and prohibition on external actions.
- MVP strategy evolution is schema-bounded declarative parameters and preregistered template IDs only; no arbitrary generated code or free prompt diff execution.
- No multi-user support, model training/fine-tuning, automatic trading/messaging/purchasing/publishing, complex graph database, or unconfirmed external knowledge auto-ingestion.
- Public repository artifacts, tests and eval fixtures use only synthetic or sanitized data. Secrets, real personal evidence, private routing config and model-call payloads never enter Git.

## Verification contract

- Every goal completes only with fresh targeted tests and evidence in the Ultragoal ledger.
- Use deterministic state-machine, authorization, deletion, crash-recovery and privacy assertions in addition to model/eval scores.
- Required code quality: Ruff, mypy, pytest, integration tests, Playwright E2E when UI exists, migration-from-empty verification, synthetic payload capture for external model calls and backup/restore rehearsal.
- Do not weaken the PRD, security invariants, confirmation requirement or hard 0/100% safety acceptance criteria to make tests pass.
- The final aggregate Codex goal remains active across intermediate stories and is completed only after the mandatory Ultragoal cleanup, invariant and independent review gate is clean.
