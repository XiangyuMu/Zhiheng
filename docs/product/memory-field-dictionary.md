# Memory Field Dictionary

This dictionary defines common fields for user memory, knowledge objects and Agent experience. It separates what is known, who asserted it, how it is authorized and where the evidence lives.

## Common Fields

| Field | Applies to | Meaning |
|---|---|---|
| `id` | all records | stable unique identifier |
| `record_type` | all records | user_memory, knowledge_object, agent_experience, strategy_release or trajectory |
| `namespace` | user memory and knowledge | candidate or formal |
| `status` | all records | lifecycle state such as candidate, formal_current, rejected, soft_deleted or erased |
| `source_kind` | all records | user_explicit, agent_inferred, imported_document, web_snapshot, tool_result, evaluated_trajectory |
| `created_by_role` | all records | user, system, proposer, validator, reviewer or publisher |
| `confidence` | inferred/evaluated records | numeric confidence with method-specific interpretation |
| `sensitivity_level` | all records | public, private, sensitive or highly_sensitive |
| `evidence_refs` | all records | links to evidence objects, spans, trajectories or review reports |
| `valid_from` | durable facts/state | when the fact starts applying |
| `valid_to` | durable facts/state | when the fact stops applying, if known |
| `version_no` | versioned records | append-only version number |
| `confirmation_generation` | serving records | generation created when user confirmation or formal publish occurred |
| `last_verified_at` | facts and strategies | last deterministic or reviewed validation time |

## User Memory Types

| Type | Description | Formalization rule |
|---|---|---|
| `identity` | stable self-description relevant to service behavior | explicit user entry or confirmed inference |
| `role` | current role or context | explicit user entry or confirmed inference |
| `goal` | desired future state or active objective | explicit user entry; inferred goals require confirmation |
| `project` | active work stream with state | explicit or imported project record |
| `preference` | likes, dislikes and recurring choices | multiple evidence points or explicit confirmation |
| `constraint` | budget, time, privacy, ethical or practical limits | explicit entry preferred; sensitive constraints require confirmation |
| `interest` | topics the user may want to explore | candidate until confirmed if inferred from behavior |
| `decision` | option, rationale, selected path and outcome | formal once user records or confirms it |

## Knowledge Object Fields

| Field | Meaning |
|---|---|
| `primary_domain_id` | one MECE primary domain from the active taxonomy |
| `title` | human-readable title |
| `object_kind` | paper, note, web_snapshot, image, book, decision_record, concept or other allowed type |
| `summary` | derived or human-authored overview; not a substitute for evidence |
| `source_quality` | primary, secondary, opinion, uncertain or mixed |
| `time_sensitivity` | stable, slowly_changing, current or expired |
| `entities` | people, organizations, papers, concepts, tools or places |
| `tags` | non-exclusive labels |
| `relations` | typed links to other objects |
| `current_version_id` | SQLite pointer to current formal version |

## Agent Experience Fields

| Field | Meaning |
|---|---|
| `task_family` | class of task the experience applies to |
| `applicability_conditions` | when to apply this experience |
| `exceptions` | cases where it should not apply |
| `procedure_ref` | Markdown or rule/template reference |
| `supporting_trajectory_ids` | successful or corrective examples |
| `counterexample_trajectory_ids` | known failures or limits |
| `risk_level` | low, medium, high or trusted_root |
| `release_state` | candidate, canary, stable, rolled_back, deprecated or archived |

## L0, L1 and L2 Memory

| Layer | Contents | Load path |
|---|---|---|
| L0 core profile | compact confirmed identity, active goals, active projects, critical constraints and safety preferences | direct structured SQLite query at task start |
| L1 topic overview | confirmed summaries for the current project or domain | structured lookup plus authorized summary artifacts |
| L2 evidence detail | raw conversations, decisions, papers, notes and spans | hybrid RAG with SQLite final authorization |

L0 and L1 can guide retrieval planning. Important judgments must be grounded in L2 evidence or clearly marked as assumption.

## Candidate Isolation Rules

- Candidate rows may be displayed for review and confirmation.
- Candidate rows cannot populate L0 or L1.
- Candidate rows cannot be used in recommendation ranking, answer strategy selection or formal summaries.
- Candidate rows cannot be indexed into serving FTS/vector generations.
- Editing a candidate creates a new candidate version; confirming it creates a formal generation.

## Redaction and Public Safety

Fields that may contain personal details or raw content must be redacted or replaced with synthetic fixtures before entering the public repository. Public examples use generic domains, synthetic text and placeholder identifiers only.
