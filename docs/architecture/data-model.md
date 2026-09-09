# SQLite Data Model

This document defines the MVP SQLite schema contract. Names are implementation-facing but may be refined in migrations if the same constraints remain true.

## Global Rules

- SQLite uses WAL mode, `busy_timeout`, foreign keys and short synchronous transactions.
- Each authoritative row has `id`, `created_at`, `updated_at` and a lifecycle/status field.
- Derived tables carry enough provenance to be rebuilt and invalidated: `source_id`, `source_version_id`, `visibility_scope`, `confirmation_generation` and processor/model version where applicable.
- Serving reads must filter through the current formal view, not direct index records.
- Long work is represented by jobs/outbox rows and executed outside DB transactions.

## Core Tables

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `evidence_objects` | `id`, `object_uri`, `sha256`, `media_type`, `byte_size`, `source_kind`, `source_metadata_json`, `status` | unique `sha256` only for non-erasable shared artifacts; no physical dedupe for erasable user evidence; index `status, created_at` |
| `content_versions` | `id`, `evidence_object_id`, `version_no`, `processor_name`, `processor_version`, `text_artifact_uri`, `content_sha256`, `status` | unique `evidence_object_id, version_no`; current chosen by SQLite pointer |
| `content_spans` | `id`, `content_version_id`, `span_kind`, `start_offset`, `end_offset`, `page_no`, `section_path`, `quote_hash` | check offsets; index `content_version_id, page_no` |
| `knowledge_objects` | `id`, `primary_domain_id`, `title`, `object_kind`, `lifecycle_status`, `visibility_scope`, `current_version_id`, `confirmation_generation`, `sensitivity_level` | index `primary_domain_id, lifecycle_status`; current formal view filters status and visibility |
| `knowledge_versions` | `id`, `knowledge_object_id`, `version_no`, `content_version_id`, `markdown_uri`, `summary`, `source_quality`, `valid_from`, `valid_to` | unique `knowledge_object_id, version_no`; immutable after publish |
| `knowledge_tags` | `knowledge_object_id`, `tag`, `tag_kind` | primary key `knowledge_object_id, tag`; tags are not primary classification |
| `entities` | `id`, `canonical_name`, `entity_type`, `aliases_json` | unique normalized canonical name per type |
| `object_entities` | `knowledge_object_id`, `entity_id`, `role`, `confidence` | composite index `entity_id, role` |
| `object_relations` | `id`, `subject_object_id`, `predicate`, `object_object_id`, `evidence_span_id`, `confidence` | no self relation unless predicate allows; index subject/object |

## User Memory Tables

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `memory_items` | `id`, `memory_type`, `subject`, `predicate`, `object_json`, `source_kind`, `status`, `confidence`, `sensitivity_level`, `valid_from`, `valid_to`, `confirmation_generation` | formal items require `status='formal_current'`; index `memory_type, status`; separate candidate namespace |
| `memory_evidence_refs` | `memory_item_id`, `evidence_object_id`, `content_span_id`, `trajectory_id`, `support_type` | support type: supporting, contradicting, origin |
| `memory_versions` | `id`, `memory_item_id`, `version_no`, `value_json`, `change_reason`, `created_by_role` | append-only; unique item/version |
| `memory_current_state` | `scope`, `state_key`, `memory_item_id`, `effective_generation` | unique scope/key; only points to formal rows |
| `confirmation_requests` | `id`, `target_type`, `target_id`, `risk_level`, `status`, `proposed_value_json`, `rationale`, `expires_at` | statuses: pending, confirmed, edited, rejected, expired |
| `confirmation_decisions` | `id`, `request_id`, `decision`, `final_value_json`, `decided_at` | immutable audit of user action |

Candidate memory rows cannot be referenced by `memory_current_state`. A database constraint or repository-level invariant test must prove this before G004 completes.

## Retrieval Tables

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `chunks` | `id`, `source_type`, `source_id`, `source_version_id`, `chunk_no`, `text`, `span_start`, `span_end`, `visibility_scope`, `confirmation_generation`, `status` | unique source/version/chunk; index `source_id, status`; formal-serving chunks only |
| `fts_chunks` | FTS5 virtual table over `title`, `segmented_text`, `raw_text` | external content table may point to `chunks`; rebuildable |
| `embedding_generations` | `id`, `model_id`, `model_revision`, `dimension`, `normalize`, `index_status`, `created_at`, `activated_at` | only one active generation per model/purpose |
| `chunk_embeddings` | `chunk_id`, `generation_id`, `embedding`, `source_version_id`, `visibility_scope`, `confirmation_generation` | unique chunk/generation; sqlite-vec adapter owns physical shape |
| `retrieval_runs` | `id`, `query_hash`, `route`, `strategy_release_id`, `created_at`, `latency_ms`, `result_count` | no raw private query in public logs |
| `retrieval_results` | `run_id`, `rank`, `source_type`, `source_id`, `source_version_id`, `score_json`, `authorized_at` | saved after SQLite final authorization |

## Jobs and Outbox

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `outbox_events` | `id`, `event_type`, `aggregate_type`, `aggregate_id`, `payload_json`, `status`, `available_at`, `attempts` | index `status, available_at`; emitted in same transaction as state change |
| `jobs` | `id`, `job_type`, `idempotency_key`, `payload_json`, `status`, `lease_owner`, `lease_expires_at`, `heartbeat_at`, `attempts`, `max_attempts` | unique `job_type, idempotency_key`; index `status, available_at` |
| `job_attempts` | `id`, `job_id`, `started_at`, `finished_at`, `status`, `error_class`, `error_message` | append-only |
| `dead_letters` | `id`, `job_id`, `payload_json`, `failure_summary`, `created_at` | manual inspection queue |

## Privacy and Model Gateway

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `sensitivity_labels` | `target_type`, `target_id`, `label`, `confidence`, `classifier_version`, `status` | uncertain labels block external transmission |
| `model_provider_configs` | `id`, `provider_kind`, `display_name`, `enabled`, `policy_json`, `secret_ref` | secrets are references only, never secret values |
| `outbound_payload_approvals` | `id`, `task_id`, `provider_id`, `payload_hash`, `classification_snapshot_id`, `redaction_snapshot_id`, `status` | only approved rows may be sent |
| `model_call_audits` | `id`, `approval_id`, `provider_id`, `model_id`, `sent_at`, `payload_hash`, `response_hash`, `status` | no raw payload storage |
| `privacy_erase_requests` | `id`, `requester`, `reason`, `status`, `created_at`, `completed_at` | irreversible after execution starts |
| `privacy_erase_ledger` | `id`, `erase_request_id`, `target_type`, `target_id`, `phase`, `status`, `before_ref_hash`, `completed_at` | write-ahead record before authoritative deletion |

## Evolution Tables

| Table | Key fields | Constraints and indexes |
|---|---|---|
| `task_trajectories` | `id`, `task_family`, `agent_version`, `knowledge_version`, `environment_version`, `status`, `evidence_refs_json` | append-only |
| `task_evaluations` | `id`, `trajectory_id`, `result_json`, `process_json`, `quality_json`, `failure_tags_json`, `confidence`, `learning_eligible` | low confidence cannot auto-enter learning set |
| `evolution_proposals` | `id`, `target_component`, `state`, `risk_level`, `minimal_diff_json`, `support_refs_json`, `counter_refs_json`, `proposer_id` | state machine enforced by domain layer |
| `validation_reports` | `id`, `proposal_id`, `fixed_set_result_json`, `dynamic_set_result_json`, `latency_cost_json`, `status` | references immutable eval set versions |
| `review_reports` | `id`, `proposal_id`, `reviewer_id`, `decision`, `rationale`, `evidence_refs_json` | reviewer cannot equal proposer |
| `strategy_releases` | `id`, `release_input_id`, `target_component`, `state`, `risk_level`, `canary_scope_json`, `rollback_target_release_id`, `activated_at` | only one stable default per target component |
| `release_inputs` | `id`, eight immutable release fields as columns, `input_sha256`, `created_at` | append-only; unique digest |

## Required Views

- `current_formal_knowledge`: formal, non-deleted knowledge objects and their current versions.
- `current_formal_memory`: confirmed current user memory only.
- `serving_chunks`: chunks whose source/version is current formal and not deleted.
- `serving_strategy_releases`: stable releases plus explicitly enabled canary releases.

These views are the final authorization boundary for retrieval, answer generation and recommendations.
