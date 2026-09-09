# User memory in answer context

Sources: PRD §11 and §12, `memory-field-dictionary.md`, and
`authority-and-lifecycle.md`. This document describes the implemented first
context boundary, not completion of the entire memory or generation feature.

## Separate channels

- L0: bounded current formal identity, role, goals, projects, safety preferences,
  and constraints, selected by structured SQLite queries.
- L1: bounded current formal items in an explicitly supplied literal dotted
  namespace (`memory_topic_prefix` on the answer request). L0 wins on overlap.
- L2: independently authorized knowledge evidence and citations. User-profile
  rows are not converted into knowledge chunks or citations.

The present L1 loader selects formal structured items; it does not implement
automatic domain selection or generated summary artifacts.

## Snapshot and serving checks

`MemoryContextService` creates immutable entries containing the formal ID,
version, confirmation generation, value, origin, sensitivity, and validity.
Snapshots bind the query hash, namespace, limit policy, selected entries and
the source-set digest. The digest is an integrity checksum, not authorization.
Authorization reloads the same complete scope from SQLite and compares the
snapshot; candidate rows are never queried. A newly added relevant constraint,
edited/deleted/expired memory, changed generation, or tampered snapshot makes
the old context invalid.

L0 is capped at 32 items and 8 KiB, L1 at 32 items, each entry at 4 KiB, and the
complete serialized snapshot at 16 KiB. Whole entries are omitted rather than
partially serialized. Counts and truncation are included in the snapshot.
The answer input budget additionally counts the context bytes conservatively.

Structured answers retain the direct-lookup path and do not invoke RAG or a
model. Other answers load memory before retrieval and validate it immediately
before generation and before accepting generated output. Database transactions
end before the model invocation. A changed snapshot returns
`memory_context_changed`; generated output is discarded if a change is found
after the call. Cached answer retries also revalidate the memory digest and
return HTTP 409 if it is stale.

## Output and audit

`personalization_refs` contains formal IDs, versions, generations and state
keys, separately from knowledge citations. The production extractive consumer
accepts the context and reports these references, while disclosing that profile
information is background rather than evidence for external facts. The query
trajectory stores references and the context digest, not memory values.

## Optional generated-answer route

Set both `ZHIHENG_ANSWER_PROVIDER_ID` and `ZHIHENG_ANSWER_MODEL_ID` to select
`GatewayAnswerModel`. Empty or half-configured bindings fail validation; leaving
both unset preserves the extractive default. Configuration does not create or
enable a provider, permit a model, or enable external transmission. The existing
provider allowlist, route rechecks and privacy gateway still apply. Sensitive
formal memory requires an approved local route; unknown sensitivity is refused.
The local route still passes the privacy pipeline, not a raw-data bypass.

The model receives separate knowledge and user-context JSON sections. Citation
inputs are checked against the supplied authorized manifest before dispatch.
On the wire, citation and memory reference IDs are stable, letters-only,
content-bound digest aliases; raw formal IDs are not model-returnable fields.
Responses must match the strict schema and reference only supplied aliases,
which are mapped back to this request's real citations and formal-memory refs.
Fresh random retrieval IDs do not change an otherwise identical wire payload,
so unresolved-dispatch protection still applies to same-content retries.
An incomplete API operation receipt returns 409 instead of retrying dispatch.

These boundaries are tested with synthetic counting transports through the real
gateway. They do not prove live-provider availability or generated-answer quality.

## Decision context boundary

Decision analysis shares `app.state.bounded_rag_service` with answers. A canonical
task includes the problem, option labels/descriptions, registered type/template,
and resolved formal goal state keys. Database IDs remain in internal provenance;
the gateway sends opaque memory aliases, never raw formal memory/version IDs.
Unknown/candidate goals are rejected, and the final canonical query binds the
same L0/L1 snapshot checks and input/output budgets used by answers.

A completed recommendation must have a nonempty answer and cited claims.
Conflicts, assumptions, insufficiencies and claim-to-citation mappings survive
API serialization, persisted-run reload and explicit formal save. The run also
records retrieval execution IDs and the stable release ID; these are execution
lineage, not raw L2 evidence or proof of semantic decision quality.

After the bounded answerer returns, every analysis publication takes
an immediate SQLite write lock and rereads memory/citation authority. The lock
covers inserting the run and completing its API receipt. An erase in the gap
after generation checks therefore causes a 409 with no new derived run, rather
than recreating erased content after the erasure sweep has already finished.
This includes model failures and budget exits: they must not resurrect erased
source IDs or citation metadata either. A changed snapshot at publication
returns 409 with a terminal failed receipt and no new decision run.

`model_calls` counts logical model/gateway invocations, including calls that
raise; it does not count confirmed external transmissions. The supplied-manifest
consumer accumulates elapsed monotonic time, checks its wall budget before
dispatch and after generation/validation, and discards over-budget output.
This synchronous port does not forcibly interrupt an already running provider;
provider transport timeouts remain a separate operational bound.

Initial save acquires a SQLite immediate write transaction before reloading and
checking current memory/citation proofs. Run identity, final authorization,
formal write and receipt completion remain under that lock. The domain save
entry point independently rejects forged/stale analyses. A successful save
retry validates the current saved decision's state key and ID rather than
requiring its original analysis context to remain unchanged.

## Still required

- Verify live local/external provider deployment and generated-answer quality;
  the API default remains local evidence extraction until explicitly configured.
- Implement automatic task-relevant L1 selection where specified and complete
  structured decision-template generation and quality evaluation.
- Implement authorized raw conversation/decision L2 evidence storage, indexing,
  outbox lifecycle and erase handling. Existing trajectory summaries are not
  original conversation evidence.
- Complete protected fixed-case coverage and independent review, then repeat
  full-scope verification. This slice does not constitute final approval.
