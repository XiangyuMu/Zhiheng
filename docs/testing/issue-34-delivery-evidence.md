# Issue #34: SHA-bound G006 delivery evidence

This index is the repository-side entry point for the final G006 delivery
evidence. Evidence is stored outside a checkout because logs, JUnit output,
browser screenshots, and API/Worker logs are generated artifacts rather than
product source. An archive is valid only when its directory name is the full
commit SHA and its top-level report repeats the same SHA.

## Resolving the final release key

The release key is the commit that introduced this index. Resolve it from a
clean checkout instead of using a mutable branch name:

```sh
release_sha="$(git log -1 --format=%H -- docs/testing/issue-34-delivery-evidence.md)"
printf '%s\n' "$release_sha"
```

The external archive entry is:

```text
${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}/
```

`ZHIHENG_DELIVERY_EVIDENCE_ROOT` is an operator-controlled durable storage
root. The archive must be copied as a whole when moved to another machine;
the report's SHA-256 manifest is the integrity check. No absolute home or
temporary-directory path is part of this index.

## Required archive layout

The final entry must contain the following files and directories:

| Path | Evidence |
| --- | --- |
| `report.json` | Same-SHA aggregate status, environment, step exit codes, elapsed time, and artifact digests |
| `versions.log` | Python, uv, npm, Playwright, restic, and package versions |
| `compile.log` | Clean-checkout compilation result |
| `ruff.log` | Ruff result |
| `mypy.log` | Full source and test Mypy result |
| `pytest/evidence.json` | Same-SHA pytest status, count, skip count, required groups, and runner wall clock |
| `pytest/pytest.log` | Complete natural pytest output |
| `pytest/pytest.xml` | JUnit report for every test case |
| `pytest/pytest-outcomes.jsonl` | Incremental per-test outcomes and failure diagnostics |
| `browser/report.json` | Browser aggregate status, commit binding, and child report digests |
| `browser/workspace-full/`, `browser/import-polling.log` | Login, text import, Worker consumption, succeeded state, search, and source return evidence |
| `browser/*.log` and child `checks.json` files | API, Worker, migration, screenshots, and all browser acceptance scenarios |

The acceptance runner is the source of the layout and must be run from a
clean checkout. The output directory must not exist before the run:

```sh
release_sha="$(git log -1 --format=%H -- docs/testing/issue-34-delivery-evidence.md)"
uv run python scripts/delivery_acceptance.py \
  --output "${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}" \
  --timeout 900
```

The command must finish naturally. A timeout, interruption, skipped test, or
report whose SHA differs from `release_sha` is a failed archive, even when an
individual child report is green.

## Historical profile evidence

The preparation optimization has one historical profile that must remain
separate from final delivery evidence:

| Commit SHA | Artifact | Meaning |
| --- | --- | --- |
| `b492d19de9538b0e893ca28989a230cc47bab262` | `issue31/profile-b492d19.json` | Cold-process G006 profile for the implementation commit; `exit_code=0`, 8 suites, 8 backups, 8 restores, 23.950 seconds |

That profile is evidence for `b492d19` only. It must never be copied into the
final SHA directory or presented as proof for a later commit. The tracked
baseline and development measurements in `docs/testing/issue-31-profile-baseline.json`
remain historical measurements with the roles stated in their own metadata.

## Acceptance matrix

The final `report.json` and child reports must make these claims independently
auditable:

| Requirement | Required evidence |
| --- | --- |
| Migration and startup | Clean-checkout migration log plus API and Worker startup logs |
| Quality gates | Compile, Ruff, full Mypy, and complete JUnit/pytest reports |
| Natural full pytest | `pytest/evidence.json`: same SHA, exit code 0, no skips, complete report, and runner wall clock at or below 900 seconds |
| Real browser import loop | Login, pasted text, persistent task, independent Worker consumption, `succeeded`, source object and index pointer, knowledge-base display, search, and source navigation |
| G006 evidence isolation | Every candidate/stage has its own evaluation run, artifact digest, trajectory, canary observation, and authorization binding |
| Backup and recovery | Real isolated backup, restore, erase, and post-restore retrieval evidence; no cross-candidate or cross-stage result reuse |

Functional correctness and the 900-second performance budget are reported as
separate fields. A passing functional report does not satisfy the performance
budget, and a fast report does not prove functional correctness.

## Verification procedure

After archiving, verify the binding from a clean checkout:

```sh
release_sha="$(git log -1 --format=%H -- docs/testing/issue-34-delivery-evidence.md)"
test "$(jq -r .sha "${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}/report.json")" = "$release_sha"
jq -e '.status == "passed" and .same_sha == true and .clean_after == true' \
  "${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}/report.json"
```

Run the Standards + Spec review against `git diff a12e588...${release_sha}`
and retain both review results beside the archive manifest. Do not close Issue
#34 or call the release complete until the index, aggregate report, and review
all identify the same SHA.
