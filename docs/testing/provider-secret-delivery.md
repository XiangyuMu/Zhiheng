# Provider secret delivery (#35–#40)

Baseline: `80aae00b3c57fa61b08e83bc689592d762e0a132` on `main`.

The Provider secret feature is delivered on `main` at commit
`366fd26143efdc7cd7c86524840d0f65524a37d8`. Each issue was implemented and
reviewed against its starting commit before the final clean-checkout gate.

| Issue | Observable delivery | Depends on | Current evidence |
| --- | --- | --- | --- |
| #36 | Settings input → encrypted persistence → restart/status → protected dispatch | None | Passed; final evidence |
| #37 | Settings rotation/deletion/connectivity → concurrency and dispatch fencing | #36 | Passed; final evidence |
| #38 | Settings migration from legacy env reference → independent encrypted secret | #36 | Passed; final evidence |
| #39 | Real backup/restore → retrieval survives lost key → credential re-entry | #36, #37 | Passed; final evidence |
| #40 | Complete browser workflow and same-commit quality/evidence gates | #37, #38, #39 | Passed; final evidence |

## Agreed test boundaries

Use SecretStore contracts, Provider HTTP integration, model gateway dispatch,
and the real settings page. Native keychain adapters have controlled backend
contract tests; report native-host smoke evidence separately from doubles.
Tests must not read or mutate existing user credentials.

Use AES-256-GCM, native macOS Keychain/Linux Secret Service, versioned
Provider-bound authenticated ciphertext, and no plaintext fallback. Provider
key entry does not enable external-model transmission or bypass outbound
approval. Existing environment references remain supported.

## Interpretation of leakage assertions

The Provider key necessarily exists transiently in its write-only submission
and the authorized transport header. No response, validation error, audit,
log, screenshot, persistent browser storage, or database record may contain
that plaintext. Ciphertext is intentionally persisted in SQLite and encrypted
backups; the prohibition on complete ciphertext applies to browser/API output,
logs, and audit responses. A literal ban on ciphertext in database queries
would contradict #35's required encrypted persistence.

Deleting a stored credential disables local dispatch; it does not revoke a
key at the external vendor or cancel a request already sent. A queued or
prepared request must recheck the current credential version before dispatch.

## Final evidence

The final report is stored outside the checkout at
`/Users/muxy/Projects/Zhiheng-delivery-evidence/366fd26b-delivery/report.json`.
It records clean-checkout migration, compile, Ruff, full Mypy, naturally
finished pytest (911 passed in 624.34 seconds), real API/Worker browser
workflow, real restic recovery, and extraction. The browser report and pytest
evidence both bind to the same SHA. Older reports, including #34 evidence,
remain historical and do not prove this feature.

## Dependency references

The implementation uses the documented AESGCM contract (256-bit key,
96-bit nonce, authenticated data, 16-byte authentication tag, and explicit
InvalidTag failure) and explicitly selects native secret storage rather than
an automatically selected fallback backend:

- https://cryptography.io/en/latest/hazmat/primitives/aead/
- https://keyring.readthedocs.io/en/stable/
- https://secretstorage.readthedocs.io/en/latest/

Linux Secret Service requires a working user D-Bus session and secret service.
An unavailable or locked backend is an observable credential error; tests must
not reinterpret it as an empty credential store or create replacement master
keys during reads.

## Browser and recovery evidence

Issue #40 is exercised by `tests/e2e/check_provider_secrets_issue40.cjs`, which drives the settings page through real API calls for creation, rotation, deletion, and legacy migration. `scripts/browser_acceptance.sh` exports only a synthetic legacy fixture value for that run and stores the resulting checks under the acceptance output directory. The backup manifest records encrypted Provider rows and the external native-keyring dependency; missing keyring material is an unavailable Provider state, not a startup or knowledge-search failure.
