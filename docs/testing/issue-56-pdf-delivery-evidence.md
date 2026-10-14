# Issues 52–56 PDF delivery evidence

This index keeps the MinerU, searchable PDF, recovery, and DeepSeek evidence
separate while binding every run to one clean-checkout commit. It is an
operator runbook, not a claim that an unexecuted external-service run passed.

## Required inputs

- A clean checkout with `git rev-parse HEAD` recorded in every report.
- `Comments.pdf` and its SHA-256 digest.
- Private `ZHIHENG_TEST_USERNAME` and `ZHIHENG_TEST_PASSWORD` variables.
- A configured MinerU gateway, Embedding route, and DeepSeek answer route.
- A durable evidence directory outside the checkout.

### Existing host gateway

The default runtime starts the Compose deployment. When the API and independent
Worker already use a host gateway and its existing persistent object store,
select that runtime explicitly before running the collector:

```bash
export ZHIHENG_MINERU_RUNTIME=host
export ZHIHENG_MINERU_GATEWAY_URL=http://127.0.0.1:9392
export ZHIHENG_MINERU_TOKEN_FILE=/absolute/private/path/mineru-token
```

The host mode performs an authenticated real `/warmup`, requires the matching
probe to succeed through `/ready`, and saves timing and readiness diagnostics.
It does not start services: the API, Worker, gateway and fault proxy must already
run the candidate SHA. Only loopback gateway origins are accepted; redirects
are rejected before any token can be forwarded. This avoids starting a second
Compose gateway over a different object store during a host-runtime acceptance.

## Issue 54 external fault controller

The browser recovery harness requires five explicitly injected scenarios at the
external parser boundary. `scripts/issue54_runtime_control.py` supplies a
repeatable HTTP proxy for that purpose. It forwards to the real MinerU service
and changes only the response for the currently armed upstream task:

```bash
python scripts/issue54_runtime_control.py serve \
  --listen 127.0.0.1:19393 \
  --upstream http://127.0.0.1:19392 \
  --state /tmp/issue54-fault-control.json
```

The private gateway runtime must be configured to use the proxy URL
(`http://127.0.0.1:19393`) before the gateway is started. The checked-in
`runtime-host-mineru/serve.sh` outside this checkout currently points directly
at `127.0.0.1:19392`; changing that private runtime configuration and starting
the proxy are operator setup steps. This controller does not restart API,
Worker, gateway, or MinerU processes.

Generate the harness scenario JSON after exporting the state path:

```bash
export ISSUE54_FAULT_CONTROL_STATE=/tmp/issue54-fault-control.json
python scripts/issue54_runtime_control.py render-scenarios \
  --command 'python scripts/issue54_runtime_control.py command --state "$ISSUE54_FAULT_CONTROL_STATE"'
```

Pass the resulting JSON as `ZHIHENG_ISSUE54_FAULTS_JSON`. For each scenario the
harness calls the command once with `ZHIHENG_ISSUE54_PHASE=prepare` before
uploading a newly generated browser task and once with `phase=proof` after an
active parser lease is observed. The controller records only proxy state and
external request observations. The proof includes the harness task and attempt
identity as context plus the observed upstream task identity; it does not
write, read, or alter Zhiheng's SQLite database.

The injected responses are deliberately narrow:

- `lost_parser_attempt`: status returns HTTP 404, producing the gateway's
  `mineru_task_not_found` terminal failure.
- `parser_failure`: status returns MinerU `failed`, producing
  `mineru_failed`.
- `partial_or_unsupported`: result returns an empty result object, producing
  `mineru_result_invalid`.
- `auto_retry_exhausted`: status returns HTTP 503 for the first upstream task,
  allowing the real Worker retry budget to exhaust.
- `manual_ui_retry`: the first upstream task returns HTTP 503; later upstream
  tasks are forwarded normally so the browser's real retry button can recover.

These scenarios prove fault handling only when the full browser command records
the resulting task state, error code, lease/retry observations, and recovery
result. A generated scenario list or a controller proof by itself is setup
evidence, not acceptance evidence.

Knowledge indexing claims use a 90-second lease with a 15-second fenced
heartbeat (`src/zhiheng/jobs/knowledge_indexing.py`); the generic repository
claim default remains 300 seconds for other callers. Issue 54 caps parse and
recovery at five minutes (300 seconds), so a crashed Worker can be reclaimed
within that budget while a live external call remains protected by heartbeats.
The controller does not change either value.

## Acceptance stages

1. `scripts/mineru_local.sh start` and `scripts/mineru_local.sh health` record
   gateway, upstream, model readiness, image, cache, and resource diagnostics.
2. `tests/e2e/check_mineru_parse.cjs` proves the real page → API → Worker →
   MinerU path, then reads the authenticated manifest projection and checks its
   schema, source digest, attempt identity, pages, blocks, and image artifacts.
3. `tests/e2e/check_pdf_search_issue53.cjs` proves the same PDF becomes formally
   searchable, records the embedding generation and physical index, and opens a
   source-linked PDF citation at the expected page.
4. `tests/e2e/check_pdf_recovery_issue54.cjs` requires explicit real Worker and
   gateway restart commands. It asserts the same task, evidence object, source
   digest, and parser attempt survive both restarts and reach a successful state.
5. `tests/e2e/check_real_files.cjs` runs the two-file answer set. It requires
   successful HTTP answers with citations for positive questions and an explicit
   evidence-insufficiency answer for the negative question. It stores source
   text, screenshots, input digests, and failure reports.
6. `scripts/delivery_acceptance.py` is the final same-SHA gate for migration,
   startup, Ruff, full Mypy, natural pytest completion, and the existing browser
   contract suite. Its functional result and the 900-second pytest budget are
   reported independently.

## Evidence rules

Each stage writes `report.json` (or `browser-report.json`) under the external
SHA-named archive. A failed run is retained with its exit code, error, logs and
screenshots. Do not copy API keys, session cookies, source files or raw model
payloads into the repository or public issues. Historical reports from another
SHA cannot satisfy a final run.

The final archive is acceptable only when migration/startup, quality gates,
the real success path, the negative answer path, both restart paths, and the
failure/retry observations all pass under the same SHA.

### Model failure evidence (#55)

The real-files report proves real DeepSeek success and negative answers. A separate
`browser/answer-failures-issue55/report.json` proves failures using an explicitly
labelled loopback HTTPS transport fixture against the real API and browser. It
injects invalid structured output and HTTP failure, checks visible safe failure,
one dispatch per request, diagnostic codes and audit/provider/model identities.
Injected failure evidence never substitutes for the real DeepSeek success report.
The collector requires both reports at the final SHA.

Transport audit success means the external request completed, not that its output
passed the answer contract. Invalid model output is recorded in the answer receipt
as `invalid_model_output`, with `answer_safety_reason_code` and the originating
`answer_audit_id`; raw model output is withheld. Transport failure retains its own
failed audit and diagnostic. There is no automatic answer retry; a user can submit
a new request, while idempotent replay does not dispatch the same request again.

The collector creates a new evidence root with mode 0700 and its own reports and
logs with mode 0600, even under a permissive shell umask. Existing run directories
are rejected so failure evidence cannot be overwritten by a later attempt.
