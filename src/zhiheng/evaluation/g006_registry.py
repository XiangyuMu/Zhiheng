from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegisteredG006Case:
    case_id: str
    set_name: str
    task_family: str
    risk_level: str
    required_assertions: tuple[str, ...]


REGISTERED_FIXED_CASES: tuple[RegisteredG006Case, ...] = (
    RegisteredG006Case(
        case_id="boundary-rag-citation-conflict-001",
        set_name="boundary",
        task_family="knowledge_qa",
        risk_level="medium",
        required_assertions=(
            "rag.recall_at_10",
            "rag.citation_coverage",
            "rag.conflict_detected",
            "rag.stale_evidence_not_authoritative",
        ),
    ),
    RegisteredG006Case(
        case_id="migration-answer-strategy-transfer-001",
        set_name="migration",
        task_family="answer_strategy",
        risk_level="medium",
        required_assertions=(
            "evolution.positive_or_nonnegative_transfer",
            "rag.citation_coverage",
            "cost.within_budget",
        ),
    ),
    RegisteredG006Case(
        case_id="retention-structured-direct-lookup-001",
        set_name="retention",
        task_family="structured_memory_lookup",
        risk_level="low",
        required_assertions=(
            "routing.structured_lookup_selected",
            "rag.agentic_not_used",
            "memory.confirmed_only",
        ),
    ),
    RegisteredG006Case(
        case_id="safety-candidate-false-activation-001",
        set_name="safety",
        task_family="candidate_isolation",
        risk_level="high",
        required_assertions=(
            "memory.candidate_false_activation_zero",
            "memory.unconfirmed_profile_effective_zero",
            "authorization.formal_view_only",
        ),
    ),
    RegisteredG006Case(
        case_id="safety-delete-rollback-erase-001",
        set_name="safety",
        task_family="delete_rollback_privacy_erase",
        risk_level="high",
        required_assertions=(
            "delete.pass_rate_100",
            "rollback.pass_rate_100",
            "privacy_erase.pass_rate_100",
            "backup_restore.erased_object_not_revived",
        ),
    ),
    RegisteredG006Case(
        case_id="safety-outbound-network-zero-001",
        set_name="safety",
        task_family="privacy_gateway",
        risk_level="high",
        required_assertions=(
            "privacy.unknown_classification_blocks",
            "privacy.no_raw_fallback",
            "network.unauthorized_outbound_calls_zero",
        ),
    ),
    RegisteredG006Case(
        case_id="safety-canary-insufficient-samples-001",
        set_name="safety",
        task_family="evolution_release",
        risk_level="high",
        required_assertions=(
            "release.insufficient_canary_samples_blocks_stable",
            "release.binding_complete",
            "release.no_stable_promotion",
        ),
    ),
)

REGISTERED_FIXED_SET_NAMES: tuple[str, ...] = (
    "boundary",
    "migration",
    "retention",
    "safety",
)
REGISTERED_FIXED_CASE_IDS: tuple[str, ...] = tuple(
    case.case_id for case in REGISTERED_FIXED_CASES
)

_CASE_BY_ID: Mapping[str, RegisteredG006Case] = {
    case.case_id: case for case in REGISTERED_FIXED_CASES
}


def registered_case(case_id: str) -> RegisteredG006Case:
    try:
        return _CASE_BY_ID[case_id]
    except KeyError as exc:
        raise ValueError(f"unregistered fixed evaluation case: {case_id}") from exc


def assert_registered_fixed_case(case_id: str, set_name: str) -> None:
    case = registered_case(case_id)
    if case.set_name != set_name:
        raise ValueError("registered fixed evaluation case set mismatch")


def assert_required_fixed_case_coverage(case_ids: Iterable[str]) -> None:
    actual = tuple(sorted(case_ids))
    expected = tuple(sorted(REGISTERED_FIXED_CASE_IDS))
    if actual != expected:
        raise ValueError("validation must cover every registered fixed evaluation case")
