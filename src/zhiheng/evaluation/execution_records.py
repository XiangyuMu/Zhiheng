"""Verification for immutable protected-run records; no evidence signing ingress."""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict
from typing import Any

from zhiheng.core.ids import sha256_json
from zhiheng.evaluation.contracts import validate_evaluation_contract
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES

# v3 also requires the real formal-memory context consumer and recommendations.
# Older signatures cannot authorize these stronger gates; rerun evaluation.
RUNNER_VERSION = "g006-proposal-execution.v3"


def verify_execution_record(row: dict[str, Any], *, secret: str) -> dict[str, Any]:
    signature = hmac.new(
        secret.encode(), (RUNNER_VERSION + "\n" + row["record_json"]).encode(), "sha256"
    ).hexdigest()
    if not hmac.compare_digest(signature, row["record_hmac"]):
        raise ValueError("execution record signature mismatch")
    record: dict[str, Any] = json.loads(row["record_json"])
    if any(record[key] != row[key] for key in ("id", "proposal_id", "idempotency_digest")):
        raise ValueError("execution record row binding mismatch")
    return record


def require_passing_execution(
    record: dict[str, Any],
    *,
    proposal_id: str,
    binding_digest: str,
    artifact_digest: str,
    expected_stage: str = "validation",
) -> None:
    contract = record.get("evaluation_contract")
    if not isinstance(contract, dict):
        raise ValueError("execution evaluation contract is missing")
    validate_evaluation_contract(contract)
    if contract.get("status") != "passed":
        raise ValueError("failed fixed assertions; evaluation contract is not promotion eligible")
    if (
        record.get("proposal_id") != proposal_id
        or record.get("binding_digest") != binding_digest
        or record.get("artifact_digest") != artifact_digest
    ):
        raise ValueError("execution proposal or artifact binding mismatch")
    if (
        record.get("runner_version") != RUNNER_VERSION
        or record.get("stage") != expected_stage
        or record.get("profile") != "local_contract"
    ):
        raise ValueError("execution runner/stage/profile mismatch")
    expected_suite = sha256_json({"cases": [asdict(case) for case in REGISTERED_FIXED_CASES]})
    if record.get("suite_digest") != expected_suite:
        raise ValueError("execution fixed suite mismatch")
    cases = record.get("cases")
    if not isinstance(cases, list) or [case.get("case_id") for case in cases] != [
        case.case_id for case in REGISTERED_FIXED_CASES
    ]:
        raise ValueError("execution must cover every registered case exactly once")
    for observed, registered in zip(cases, REGISTERED_FIXED_CASES, strict=True):
        assertions = observed.get("assertions", [])
        if observed.get("set_name") != registered.set_name or sorted(
            item.get("name", "") for item in assertions
        ) != sorted(registered.required_assertions):
            raise ValueError("execution assertion coverage mismatch")
        if observed.get("failure_tags") or any(
            item.get("passed") is not True for item in assertions
        ):
            raise ValueError("execution contains failed fixed assertions")
    report = record.get("report", {})
    if report.get("promotion_eligible") is not True or report.get("failure_count") != 0:
        raise ValueError("execution report is not promotion eligible")
