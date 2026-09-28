# Issue 30 Asset Audit

## Scope

This audit covers the repository-owned asset cleanup slice for `.gitignore`, tracked `.omx` files, and PDF fixture references.

## Tracked asset findings

- `.omx/ultragoal/brief.md` is retained because `docs/testing/g006-real-execution-handoff.md` and `docs/testing/remaining-invariant-gaps.md` cite it as historical acceptance and invariant evidence.
- `.omx/ultragoal/goals.json` is retained because it summarizes the completed durable goals and points at the retained brief.
- `.omx/ultragoal/quality-gate.json` is retained because it is the compact final quality-gate evidence and names the retained source artifacts.
- `.omx/ultragoal/ledger.jsonl` is removed from Git tracking with the local file preserved. It is a large runtime audit log and is not directly referenced by repository tests or docs.
- `.omx/ultragoal/codex-goal-complete.json` is removed from Git tracking with the local file preserved. It is a local goal-mode completion marker rather than product evidence.

## Untracked and ignored asset findings

`git status --short --ignored` showed only ignored local runtime artifacts, including `.DS_Store`, virtualenv/cache directories, `__pycache__`, local SQLite databases, `.omx/state/`, `.omx/tmux-hook.json`, `node_modules/`, and `var/`.

The ignore policy now treats `.omx` as runtime-local by default while allowing only the retained historical Ultragoal evidence files listed above.

## PDF fixture references

The tracked PDF fixture files remain intentional test assets:

- `tests/fixtures/pdf/pdf-parser-manifest-example.json` is read by `tests/unit/test_pdf_manifest.py`.
- `tests/fixtures/pdf/mixed-layout.expected.json` is read by `tests/unit/test_pdf_worker.py` and `tests/unit/test_pdf_mixed_fixture.py`.
- `tests/fixtures/pdf/mixed_layout_fixture.py` builds the byte-stable synthetic PDF used by `tests/unit/test_pdf_mixed_fixture.py`.

No fixture path moves were needed.

## Product inventory and delivery boundary

At the start of #30 the `main` checkout was clean at `d27c792`; there were no
uncommitted PDF migrations to recover. The manifest migration from #22 is already
tracked. `src/`, `migrations/`, `schemas/`, `tests/`, `benchmarks/`, `scripts/`,
`deploy/`, dependency lockfiles and CI configuration are retained product or
verification assets. `docs/`, `CONTEXT.md`, `DESIGN.md`, and `AGENTS.md` preserve
requirements, decisions, design and repository instructions. Existing deployment
configuration is an implementation asset, not proof of a production deployment.

The tracked-path audit found no database, cache or runtime log outside the two
OMX runtime files removed from the index. JSON/JSONL files under test fixtures and
`docs/testing/issue-27-evidence.json` are synthetic fixtures or historical evidence,
not disposable cache. No global skills, private data, `.env`, local database,
ignored runtime files or unrelated directories were deleted or modified.
The retained historical goals file contains an old `ledgerPath`; it is provenance,
not a runtime dependency. Old quality-gate results are historical and cannot sign
off the new release. Git history preserves both removed files for reconstruction.

New delivery documents and final evidence are reviewed separately. The definitive
scope is `git diff d27c792...<final SHA>` and the final same-SHA report required by
[release notes](../releases/0.1.0.md).
