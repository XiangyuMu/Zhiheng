# Security and Privacy Evaluation Cases

## Scope

These cases protect the single-user knowledge base, user memory, RAG pipeline, model gateway, and evolution release flow. They are contract cases until product code exists.

## Required Case Groups

| Group | Required Acceptance |
|---|---|
| Candidate isolation | Unconfirmed inferred profile changes and external knowledge candidates never affect formal context, answers, or recommendations |
| Delete and rollback | Delete, restore, and rollback pass every fixture case |
| Privacy erase | Erase uses write-ahead ledger semantics and cannot be revived by index rebuild or backup restore |
| Prompt injection | External documents, webpages, tool output, and model output are data, never system instructions |
| External model gateway | No raw data leaves the system without an approved outbound payload |
| Sensitive routing | Unknown classification, uncertain redaction, or failed recheck blocks external transmission |
| External actions | Trading, messaging, purchasing, publishing, and sending remain impossible in MVP |
| Release safety | Candidate release is blocked by safety failure, missing binding, insufficient canary evidence, or reviewer/proposer collision |

## Mandatory Assertions

- Candidate false activation count is always `0`.
- Unauthorized outbound network/model call count is always `0` in safety tests.
- Delete, restore, rollback, and privacy erase cases have `100%` pass requirements.
- Safety failures block canary promotion and stable publication.
- Canary sample shortage blocks stable promotion even when all observed samples pass.
- Reviewer and proposer identities must differ.
- Release binding contains all eight immutable fields.
- Public fixtures contain only synthetic or sanitized examples.

## Synthetic Examples

The contract fixture uses fictional topics, people, and documents:

- a synthetic paper note about vector indexing;
- a synthetic personal preference candidate;
- a synthetic finance-learning goal without account numbers;
- a synthetic external article containing prompt-injection text;
- synthetic deletion, rollback, and backup-restore identifiers.

These examples are intentionally generic. Real personal evidence belongs in private deployment storage and must not be committed.

## Privacy Scan Baseline

Contract fixtures must be scanned for common accidental leaks:

- email addresses;
- API keys and bearer tokens;
- private SSH keys;
- Chinese mobile numbers;
- US Social Security numbers;
- credit card-like long digit groups;
- repository-private model payload dumps.

The scan is a baseline. It does not replace manual review for real-world privacy safety.
