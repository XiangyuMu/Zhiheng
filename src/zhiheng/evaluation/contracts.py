"""Canonical, provider-safe evaluation evidence contract.

The evaluation contract is deliberately independent from a particular runner.
Fixtures, protected runs, validation reports, review artifacts and release
records all carry the same canonical object and digest it before binding it to
another artifact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from zhiheng.core.ids import sha256_json

EVALUATION_CONTRACT_SCHEMA = "evaluation_contract.v1"
REGRESSION_REPORT_SCHEMA = "regression_report.v1"
RELEASE_BINDING_SCHEMA = "step0.release_binding.v1"

REQUIRED_FIELDS = (
    "schema_version",
    "fixture_set_digest",
    "input_set_digest",
    "policy_threshold_digest",
    "runner_environment_digest",
    "generated_at",
    "providers",
    "case_outcomes",
    "aggregate_metrics",
    "status",
    "blocked_reasons",
)
RELEASE_BINDING_FIELDS = (
    "candidate_id",
    "target_component",
    "source_evaluation_ids",
    "source_evidence_refs",
    "validation_report_ref",
    "reviewer_decision_ref",
    "approved_artifact_digest",
    "rollback_target_id",
)
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_CASE_STATUSES = {"passed", "failed", "blocked", "skipped", "timeout", "unavailable"}
_BLOCKING_STATUSES = {"blocked", "skipped", "timeout", "unavailable"}
_SECRET_KEYS = {"api_key", "authorization", "secret", "token", "credential", "password"}
REGRESSION_SECTIONS = (
    "retrieval",
    "citations",
    "candidate_isolation",
    "privacy",
    "unsupported_claims",
    "external_actions",
)


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return the one JSON representation used for contract digests."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_mapping(value: Mapping[str, Any]) -> str:
    return f"sha256:{sha256_json(dict(value))}"


def build_evaluation_contract(
    *,
    fixture_set_digest: str,
    input_set_digest: str,
    policy_threshold_digest: str,
    runner_environment_digest: str,
    providers: Sequence[Mapping[str, Any]],
    case_outcomes: Sequence[Mapping[str, Any]],
    aggregate_metrics: Mapping[str, Any],
    generated_at: str | None = None,
    blocked_reasons: Sequence[str] = (),
) -> dict[str, Any]:
    """Build and validate an immutable ``evaluation_contract.v1`` object."""

    contract = {
        "schema_version": EVALUATION_CONTRACT_SCHEMA,
        "fixture_set_digest": fixture_set_digest,
        "input_set_digest": input_set_digest,
        "policy_threshold_digest": policy_threshold_digest,
        "runner_environment_digest": runner_environment_digest,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "providers": [dict(item) for item in providers],
        "case_outcomes": [dict(item) for item in case_outcomes],
        "aggregate_metrics": dict(aggregate_metrics),
        "status": _aggregate_status(case_outcomes, blocked_reasons),
        "blocked_reasons": list(blocked_reasons),
    }
    validate_evaluation_contract(contract)
    contract["contract_digest"] = contract_digest(contract)
    return contract


def contract_digest(contract: Mapping[str, Any]) -> str:
    payload = dict(contract)
    payload.pop("contract_digest", None)
    return digest_mapping(payload)


def build_regression_report(
    contract: Mapping[str, Any],
    *,
    report_id: str,
    section_metrics: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the complete sanitized report consumed by review and release gates.

    The report deliberately contains only canonical contract evidence and
    aggregate section metrics. Raw prompts, documents, model payloads and
    provider secrets are never accepted as report fields.
    """

    validate_evaluation_contract(contract)
    if not isinstance(report_id, str) or not report_id.strip():
        raise ValueError("regression report report_id is required")
    if set(section_metrics) != set(REGRESSION_SECTIONS):
        raise ValueError("regression report must contain all required sections")
    report = {
        "schema_version": REGRESSION_REPORT_SCHEMA,
        "report_id": report_id,
        "contract_digest": contract_digest(contract),
        "status": contract["status"],
        "sanitized": True,
        "sections": {name: dict(section_metrics[name]) for name in REGRESSION_SECTIONS},
        "case_outcomes": [dict(item) for item in contract["case_outcomes"]],
        "aggregate_metrics": dict(contract["aggregate_metrics"]),
        "blocked_reasons": list(contract["blocked_reasons"]),
        "generated_at": contract["generated_at"],
    }
    validate_regression_report(report)
    report["report_digest"] = digest_mapping(report)
    return report


def validate_regression_report(
    report: Mapping[str, Any],
    *,
    expected_contract_digest: str | None = None,
    require_passing: bool = False,
) -> None:
    """Reject incomplete, unsanitized or stale full-regression artifacts."""

    required = {
        "schema_version",
        "report_id",
        "contract_digest",
        "status",
        "sanitized",
        "sections",
        "case_outcomes",
        "aggregate_metrics",
        "blocked_reasons",
        "generated_at",
    }
    missing = required.difference(report)
    if missing:
        raise ValueError(f"regression report missing fields: {', '.join(sorted(missing))}")
    if report["schema_version"] != REGRESSION_REPORT_SCHEMA:
        raise ValueError("unsupported regression report schema")
    if not isinstance(report["report_id"], str) or not report["report_id"]:
        raise ValueError("regression report report_id is required")
    _require_digest(report["contract_digest"], "contract_digest")
    if (
        expected_contract_digest is not None
        and report["contract_digest"] != expected_contract_digest
    ):
        raise ValueError("regression report contract digest mismatch")
    if report["sanitized"] is not True:
        raise ValueError("regression report must be sanitized")
    sections = report["sections"]
    if not isinstance(sections, Mapping) or set(sections) != set(REGRESSION_SECTIONS):
        raise ValueError("regression report sections are incomplete")
    if any(not isinstance(metrics, Mapping) for metrics in sections.values()):
        raise ValueError("regression report section metrics must be objects")
    if report["status"] not in {"passed", "failed", "blocked"}:
        raise ValueError("regression report status is invalid")
    if not isinstance(report["case_outcomes"], list) or not report["case_outcomes"]:
        raise ValueError("regression report case_outcomes are required")
    if not isinstance(report["aggregate_metrics"], Mapping):
        raise ValueError("regression report aggregate_metrics must be an object")
    if not isinstance(report["blocked_reasons"], list):
        raise ValueError("regression report blocked_reasons must be a list")
    if not isinstance(report["generated_at"], str) or not report["generated_at"]:
        raise ValueError("regression report generated_at is required")
    serialized = canonical_json(report)
    if any(re.search(rf"\b{re.escape(key)}\b", serialized, re.IGNORECASE) for key in _SECRET_KEYS):
        raise ValueError("regression report contains secret material")
    supplied_digest = report.get("report_digest")
    if supplied_digest is not None:
        _require_digest(supplied_digest, "report_digest")
        payload = dict(report)
        payload.pop("report_digest", None)
        if supplied_digest != digest_mapping(payload):
            raise ValueError("regression report digest mismatch")
    if require_passing and report["status"] != "passed":
        raise ValueError("regression report is not passing")


def validate_evaluation_contract(
    contract: Mapping[str, Any],
    *,
    expected_digest: str | None = None,
    require_passing: bool = False,
) -> None:
    """Fail closed on incomplete, non-canonical or silently skipped evidence."""

    missing = [field for field in REQUIRED_FIELDS if field not in contract]
    if missing:
        raise ValueError(f"evaluation contract missing fields: {', '.join(missing)}")
    if contract.get("schema_version") != EVALUATION_CONTRACT_SCHEMA:
        raise ValueError("unsupported evaluation contract schema")
    for field in (
        "fixture_set_digest",
        "input_set_digest",
        "policy_threshold_digest",
        "runner_environment_digest",
    ):
        _require_digest(contract.get(field), field)
    generated_at = contract.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("evaluation contract generated_at is required")
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("evaluation contract generated_at must be ISO-8601") from exc

    providers = contract.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("evaluation contract providers are required")
    for provider in providers:
        if not isinstance(provider, Mapping):
            raise ValueError("evaluation contract provider must be an object")
        if any(str(key).lower() in _SECRET_KEYS for key in provider):
            raise ValueError("evaluation contract provider contains secret material")
        for field in ("provider_id", "model_id", "availability"):
            if not isinstance(provider.get(field), str) or not provider[field]:
                raise ValueError(f"evaluation contract provider {field} is required")

    outcomes = contract.get("case_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        raise ValueError("evaluation contract case_outcomes are required")
    seen: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            raise ValueError("evaluation contract case outcome must be an object")
        case_id = outcome.get("case_id")
        status = outcome.get("status")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("evaluation contract case ids must be unique nonempty strings")
        seen.add(case_id)
        if status not in _CASE_STATUSES:
            raise ValueError(f"unsupported evaluation case status: {status!r}")
        if type(outcome.get("passed")) is not bool:
            raise ValueError("evaluation case outcome passed must be boolean")
        if status == "passed" and outcome["passed"] is not True:
            raise ValueError("passed evaluation outcome must have passed=true")
        if status != "passed" and outcome["passed"] is True:
            raise ValueError("non-passed evaluation outcome cannot have passed=true")
        if status in _BLOCKING_STATUSES and not outcome.get("reason"):
            raise ValueError(f"{status} evaluation outcome requires a reason")

    blocked_reasons = contract.get("blocked_reasons")
    if not isinstance(blocked_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in blocked_reasons
    ):
        raise ValueError("blocked_reasons must be a list of nonempty strings")
    status = contract.get("status")
    if status not in {"passed", "failed", "blocked"}:
        raise ValueError("evaluation contract status must be passed, failed or blocked")
    outcome_statuses = {str(outcome["status"]) for outcome in outcomes}
    has_blocker = bool(outcome_statuses & _BLOCKING_STATUSES) or bool(blocked_reasons)
    has_failure = any(outcome["status"] == "failed" for outcome in outcomes)
    expected_status = "blocked" if has_blocker else ("failed" if has_failure else "passed")
    if status != expected_status:
        raise ValueError("evaluation contract aggregate status is inconsistent")
    metrics = contract.get("aggregate_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("evaluation contract aggregate_metrics must be an object")
    if status == "passed" and any(
        metrics.get(key, 0) != 0
        for key in ("candidate_false_activation", "privacy_leak_count", "external_action_count")
    ):
        raise ValueError("zero-tolerance evaluation gate failed")
    if require_passing and status != "passed":
        raise ValueError("evaluation contract is not promotion eligible")

    supplied_digest = contract.get("contract_digest")
    if supplied_digest is not None:
        _require_digest(supplied_digest, "contract_digest")
        if supplied_digest != contract_digest(contract):
            raise ValueError("evaluation contract digest mismatch")
    if expected_digest is not None and expected_digest != contract_digest(contract):
        raise ValueError("evaluation contract does not match expected digest")


def bind_release_contract(
    contract: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    report_digest: str,
) -> dict[str, Any]:
    """Create the release-facing immutable binding around contract evidence."""

    validate_evaluation_contract(contract, require_passing=True)
    actual_fields = tuple(field for field in binding if field != "schema_version")
    if set(actual_fields) != set(RELEASE_BINDING_FIELDS) or len(actual_fields) != len(
        RELEASE_BINDING_FIELDS
    ):
        raise ValueError("release binding must contain exactly the eight required fields")
    if binding.get("schema_version", RELEASE_BINDING_SCHEMA) != RELEASE_BINDING_SCHEMA:
        raise ValueError("unsupported release binding schema")
    _require_digest(report_digest, "report_digest")
    bound = {
        "schema_version": EVALUATION_CONTRACT_SCHEMA,
        "release_binding": dict(binding),
        "evaluation_contract_digest": contract_digest(contract),
        "input_set_digest": contract["input_set_digest"],
        "report_digest": report_digest,
    }
    bound["binding_digest"] = digest_mapping(bound)
    return bound


def build_fixed_suite_contract(
    *,
    fixture_set_digest: str,
    runner_environment_digest: str,
    cases: Sequence[Any],
    aggregate_metrics: Mapping[str, Any],
    policy_threshold_digest: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Adapt a protected fixed-suite run into the shared contract shape."""

    outcomes: list[dict[str, Any]] = []
    for case in cases:
        passed = bool(getattr(case, "passed", False))
        failure_tags = list(getattr(case, "failure_tags", ()))
        status = "passed" if passed else "failed"
        reason = ", ".join(failure_tags) if failure_tags else None
        outcome = {
            "case_id": str(case.case_id),
            "set_name": str(case.set_name),
            "status": status,
            "passed": passed,
            "failure_tags": failure_tags,
        }
        if reason:
            outcome["reason"] = reason
        outcomes.append(outcome)
    return build_evaluation_contract(
        fixture_set_digest=fixture_set_digest,
        input_set_digest=fixture_set_digest,
        policy_threshold_digest=policy_threshold_digest,
        runner_environment_digest=runner_environment_digest,
        providers=(
            {
                "provider_id": "protected-local",
                "model_id": "protected-fixed-suite",
                "availability": "available",
            },
        ),
        case_outcomes=outcomes,
        aggregate_metrics=aggregate_metrics,
        generated_at=generated_at,
    )


def _aggregate_status(outcomes: Sequence[Mapping[str, Any]], blocked_reasons: Sequence[str]) -> str:
    statuses = {str(item.get("status", "")) for item in outcomes}
    if statuses & _BLOCKING_STATUSES or blocked_reasons:
        return "blocked"
    if "failed" in statuses:
        return "failed"
    return "passed"


def _require_digest(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be a sha256 digest")
