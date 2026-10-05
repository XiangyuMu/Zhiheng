# Issue #34: SHA-bound G006 delivery evidence

This index is the repository-side entry point for the final G006 delivery
evidence. Evidence is stored outside a checkout because logs, JUnit output,
browser screenshots, and API/Worker logs are generated artifacts rather than
product source. An archive is valid only when its directory name is the full
commit SHA and its top-level report repeats the same SHA.

## Resolving the final release key

The release key is the last commit that updated this index. All final code
fixes must be included in that commit or an ancestor; updating product code
afterward requires updating this index and generating a new archive. Resolve it from a
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
| `report.json` | Same-SHA acceptance status, step exit codes, elapsed time, and artifact digests |
| `versions.log` | Installed Python package versions (`uv pip freeze`) |
| `environment.json` | OS, CPU, Python, uv, Node, npm, Playwright, and restic versions |
| `archive-summary.json` | SHA, independent functional/performance verdicts, historical archive reference, and SHA-256 digests of every archived file except itself |
| `g006-profile.json`, `g006-profile.log` | Fresh final-SHA profile with candidate/stage evaluation and authorization evidence |
| `reviews/standards.md`, `reviews/spec.md` | Final review scope, SHA, findings, and verdict |
| `compile.log` | Clean-checkout compilation result |
| `ruff.log` | Ruff result |
| `mypy.log` | Full source and test Mypy result |
| `pytest/evidence.json` | Same-SHA pytest status, environment, required groups, and runner wall clock (counts in JUnit) |
| `pytest/pytest.log` | Complete natural pytest output |
| `pytest/pytest.xml` | JUnit report for every test case |
| `pytest/pytest-outcomes.jsonl` | Incremental per-test outcomes and failure diagnostics |
| `browser/report.json` | Browser aggregate status, commit binding, and child report digests |
| `browser/workspace-full/`, `browser/import-polling.log` | Login, text import, Worker consumption, succeeded state, search, and source return evidence |
| `browser/*.log` and child `checks.json` files | API, Worker, migration, screenshots, and all browser acceptance scenarios |

The acceptance runner generates the acceptance reports. The environment,
profile, review records, and archive summary are added afterward without
rewriting the original acceptance reports. Run from a clean checkout whose
HEAD equals `release_sha`. The output directory must not exist before the run:

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
| `b492d19de9538b0e893ca28989a230cc47bab262` | `historical/b492d19de9538b0e893ca28989a230cc47bab262/profile-b492d19.json` | Cold-process G006 profile for the implementation commit; `exit_code=0`, 8 suites, 8 backups, 8 restores, 23.950 seconds |

The historical archive identifier is
`historical/b492d19de9538b0e893ca28989a230cc47bab262/` relative to
`ZHIHENG_DELIVERY_EVIDENCE_ROOT`. It contains both original files and
`manifest.json`. Verified checksums:

- `profile-b492d19.json`: `eeffc6f23af887d3142b58e0804089ad753c69008002592845e9affd248fa59d`
- `profile-b492d19.log`: `a1a62f58546bd5a9ad202e33695ba36b3fd2a02171b7881ecfbe5dfccb87371f`

The historical profile reports macOS 27.0.1 arm64 / Apple M4. It is a
profile-only measurement; it does not prove a full delivery run at that SHA.

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

In `archive-summary.json`, `functional_gate` records acceptance/profile
results, and `pytest_budget` separately records the runner wall time, the
900-second limit, and its verdict. `review_gate` records both review verdicts. A passing functional report does not satisfy the performance
budget, and a fast report does not prove functional correctness.

The startup matrix treats dependency `SyntaxWarning` lines as incidental
stderr noise while asserting the final stable Worker configuration error line;
this keeps the clean-checkout contract independent of the Python patch level.

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

## Completing and verifying an archive

After acceptance, run the profile on the same clean checkout:

```sh
uv run python scripts/profile_g006_preparation.py \
  --output "${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}/g006-profile.json" \
  > "${ZHIHENG_DELIVERY_EVIDENCE_ROOT}/${release_sha}/g006-profile.log" 2>&1
```

Record the command exit code. Check that the profile SHA equals `release_sha`,
its exit code is zero, and it contains fresh `suite_evidence`,
`execution_evidence`, and `authorization_evidence`. Inspect the candidate/stage
bindings, unique run/trajectory identifiers, report digests, canary observations,
and authorized release identifiers. A previous profile cannot fill missing
fields in the final run.

Record the current environment, then preserve both completed reviews. Write
`archive-summary.json` last, with `sha`, `environment`, `functional_gate`,
`pytest_budget`, `review_gate`, `historical_archive`, and `artifacts`. Its
`artifacts` map uses archive-relative paths and SHA-256 values, including the
unaltered acceptance `report.json`, profile, environment, and reviews. Verify
every digest by reading the archived file, as well as each child report's
manifest. API/Worker shutdown must finish before the browser report hashes
logs, so shutdown messages cannot invalidate its checksums. Reject a missing file or mismatch. The archive summary excludes
itself to avoid a circular digest. Copy the historical directory together with
the final archive when transferring evidence.

The index defines the archive key and verification contract; measured final
counts, timings, environment, and verdicts live in that key's archive summary.
This avoids modifying the tested commit merely to paste its generated results.
