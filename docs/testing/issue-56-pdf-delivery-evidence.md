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
