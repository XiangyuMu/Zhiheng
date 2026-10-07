# ADR 0006: Local encrypted Provider secrets and recovery

- **Status:** accepted
- **Date:** 2026-10-06

## Decision

Provider keys entered in the settings page are encrypted with AES-256-GCM before they are written to `provider_secret_records`. The instance master key is held by the platform keyring (macOS Keychain or Linux Secret Service) and is never written to the database, backup bundle, browser, or logs. API responses expose only `secret_status`, `secret_source`, `secret_version`, and a short fingerprint.

Legacy `env:ZHIHENG_PRIVATE_*` references remain readable and can be explicitly migrated. Migration resolves the environment value once, stores a new encrypted version, and leaves the legacy reference unchanged if resolution fails. Rotation revokes previous local versions. Deleting a key revokes every local version for the Provider and disables it.

Backups include the encrypted database records and a manifest declaration that the native keyring is an independent recovery dependency. Restoring without the master key keeps database search and source-object access available; affected Providers are shown as unavailable and must be re-entered. The restore command never falls back to plaintext or copies keyring material into restic.

Connectivity audits and secret lifecycle audits use an explicit `audit_kind`
field. Rotation, revocation, and migration history is exposed through the
separate authenticated secret-audit query and never relies on a sentinel model
identifier.

## Evidence boundary

Provider Secret acceptance evidence must come from the same clean-checkout commit and include migration, API/Worker startup, browser operations, restart or restore behavior, and leakage checks across page text, browser storage, HTTP responses, logs, and database queries. Historical browser or evaluation evidence is not reused.
