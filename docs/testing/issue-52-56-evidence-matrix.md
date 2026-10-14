# Issues 52–56 evidence matrix

This matrix is the checked-in contract for the final delivery gate. It maps
each PDF acceptance issue to one independently produced report under the
same clean-checkout SHA. A report from another commit, a synthetic fixture, or
an unexecuted command cannot satisfy the mapped row.

The machine-readable source is
[`issue-52-56-evidence-matrix.json`](issue-52-56-evidence-matrix.json). The
delivery runner validates that all five issue rows are present before it
starts any external-service stage and fails if a required report is missing or
does not identify the expected SHA.

| Issue | Stage | Required report | Evidence boundary |
| --- | --- | --- | --- |
| #52 | PDF parse | `pdf-parse/report.json` | Browser upload → API → Worker → MinerU manifest and readable artifacts |
| #53 | PDF search | `pdf-search/report.json` | Embedding/index qualification, library hit, source-linked page |
| #54 | Recovery | `pdf-recovery/report.json` | Active Worker/gateway restart, five injected faults, retry/UI evidence |
| #55 | Real answers | `real-files/browser-report.json` | Positive cited answers and negative insufficiency behavior; separately labelled `browser/answer-failures-issue55/report.json` proves fault diagnostics and bounded dispatch |
| #56 | Delivery | `report.json` | Same SHA, migrations/startup, quality gates, natural pytest and stage timings |

Private source files, credentials, cookies, raw model payloads and unredacted
logs remain in the external evidence directory and are never copied into this
repository.
