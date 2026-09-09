"""Old signed records cannot stand in for newly required component execution."""

import hmac
import json

import pytest

from zhiheng.evaluation.execution_records import verify_execution_record


@pytest.mark.parametrize("legacy_version", [
    "g006-proposal-execution.v1", "g006-proposal-execution.v2",
])
def test_pre_dispatch_evidence_signature_is_not_current_execution_evidence(
    legacy_version: str,
) -> None:
    secret = "synthetic-record-version-test"
    record = {
        "id": "synthetic-run", "proposal_id": "synthetic-proposal",
        "idempotency_digest": "synthetic-key", "runner_version": legacy_version,
    }
    encoded = json.dumps(record)
    row = {
        **record,
        "record_json": encoded,
        "record_hmac": hmac.new(
            secret.encode(), (legacy_version + "\n" + encoded).encode(), "sha256",
        ).hexdigest(),
    }
    with pytest.raises(ValueError, match="signature"):
        verify_execution_record(row, secret=secret)
