# System Overview

This document defines the implementable MVP system boundary for Zhiheng, a single-user self-evolving personal knowledge Agent. It is public-safe and uses only generic examples.

## Scope

Zhiheng runs as a modular monolith deployed on one local or cloud server:

- API process: FastAPI HTTP API, authentication, authorization, user-facing commands, query orchestration and confirmation workflows.
- Worker process: background ingestion, parsing, embedding, index rebuilds, evaluation jobs, privacy erase execution and scheduled evolution runs.
- Shared domain/application code: entities, policies, state machines, repositories, privacy gateway and retrieval contracts are shared by API and Worker.

MVP does not support multi-user tenancy, external automatic actions, arbitrary generated code execution, model fine-tuning, microservices, external brokers, graph databases or independent vector databases.

## Component Boundaries

| Component | Owns | Must not own |
|---|---|---|
| API | HTTP contracts, session checks, CSRF checks, request validation, user confirmation, read orchestration, response assembly | Long parsing, model calls inside DB transactions, direct provider calls, direct index visibility decisions |
| Worker | Idempotent jobs, ingestion pipelines, derivative generation, scheduled evaluation, privacy erase replay | Browser session authority, user approval decisions, serving stale index records without SQLite reauthorization |
| Domain | lifecycle rules, confirmation gates, capability checks, state machines, release rules, deletion policy | vendor SDK details, filesystem layout assumptions, UI rendering |
| Infrastructure | SQLite repositories, object store, Markdown store, FTS5, sqlite-vec, model adapters, backup adapters | product policy decisions, bypasses around privacy or confirmation gates |
| UI | confirmation center, knowledge browsing, source/citation visibility, settings | direct database writes, raw secret handling, direct external model calls |

## Runtime Flow

1. The API receives an authenticated single-user request.
2. The API loads structured state from SQLite and selects direct lookup, hybrid RAG or bounded Agentic RAG.
3. Retrieval components may query FTS5 and sqlite-vec, but every candidate must be reauthorized against the SQLite current formal view before entering model context.
4. If model use is needed, the request passes through the privacy gateway. Unknown classification, uncertain redaction, missing provider approval or unavailable required local model returns an explained refusal.
5. User-facing answers cite authorized evidence spans and distinguish knowledge evidence, user memory, model inference and unverified assumptions.
6. Any long-running work is committed as a short SQLite transaction that writes state plus outbox/job rows, then the Worker performs external or expensive work outside the transaction.

## Storage Roles

| Store | Role | Authority |
|---|---|---|
| Evidence object store | Original uploaded bytes, snapshots, images and immutable derived text artifacts | Authoritative for original content bytes |
| Markdown store | Human-readable notes, ADRs, strategy specs and experience documents | Authoritative only for declared document bodies and strategy artifacts |
| SQLite | lifecycle, current pointers, visibility, confirmation generation, processing state, relationship metadata and release state | Authoritative for structured truth and serving authorization |
| FTS5 | lexical retrieval over approved text fields | Derived and rebuildable |
| sqlite-vec | semantic retrieval over approved chunk embeddings | Derived and rebuildable |
| Summaries/caches | speed and display optimization | Derived and never authoritative |

## Transaction Rules

- SQLite runs in WAL mode with a configured busy timeout.
- API and Worker use short synchronous transactions.
- Transactions may include metadata, lifecycle state, current pointers, outbox rows and audit rows.
- Transactions must not include network calls, model calls, parsing, embedding, large file I/O or long evaluation work.
- Cross-storage writes use immutable-object-first commit, then SQLite pointer/outbox commit. Orphaned immutable objects are cleaned by GC only after they are proven unreferenced.

## Public-Safe Repository Rules

Public repository artifacts may include product docs, architecture docs, source code, migrations, synthetic tests and sanitized benchmark fixtures. They must not include real personal evidence, real user profile details, secrets, provider routing configuration or raw external model payloads.
