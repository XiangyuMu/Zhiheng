from pathlib import Path
from typing import Any

import pytest

from zhiheng.evaluation.g006_runner import (
    EvaluationSubject,
    G006ExecutionStage,
    ProtectedFixedSuiteRunner,
)
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.gaps import KnowledgeGapService
from zhiheng.memory import MemoryRepository
from zhiheng.query.service import QueryAnswerService
from zhiheng.retrieval import QueryRoute, StructuredLookupService


def test_runner_rejects_artifact_changed_after_digest_binding(tmp_path: Path) -> None:
    artifact = default_release_artifact()
    subject = EvaluationSubject.from_artifact(
        candidate_id="mutation-probe",
        target_component="retrieval.answer_strategy",
        artifact_payload=artifact,
    )
    subject.artifact_payload["routing"]["route_override"] = "hybrid"
    with pytest.raises(ValueError, match="digest"):
        ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
            subject,
            stage=G006ExecutionStage.VALIDATION,
            work_dir=tmp_path,
            case_id="retention-structured-direct-lookup-001",
        )


def test_retention_assertion_depends_on_actual_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = EvaluationSubject.from_artifact(
        candidate_id="lookup-probe", target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    monkeypatch.setattr(StructuredLookupService, "lookup", lambda *args, **kwargs: [])
    observed = ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
        case_id="retention-structured-direct-lookup-001",
    )
    assert not observed.passed
    assert "assertion_failed:memory.confirmed_only" in observed.failure_tags
    assert observed.observed_facts["structured_lookup_row_count"] == 0


def test_candidate_isolation_detects_context_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = EvaluationSubject.from_artifact(
        candidate_id="context-leak-probe", target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    monkeypatch.setattr(
        MemoryRepository, "l0_context",
        lambda *args: {"goal.unconfirmed": {"text": "unconfirmed-profile-sentinel"}},
    )
    observed = ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
        case_id="safety-candidate-false-activation-001",
    )
    assert not observed.passed
    assert "assertion_failed:memory.unconfirmed_profile_effective_zero" in (
        observed.failure_tags
    )


def test_migration_executes_paired_inputs_and_detects_candidate_regression(tmp_path: Path) -> None:
    artifact = default_release_artifact()
    artifact["routing"]["route_override"] = "structured"
    subject = EvaluationSubject.from_artifact(
        candidate_id="migration-regression", target_component="retrieval.answer_strategy",
        artifact_payload=artifact, baseline_artifact_payload=default_release_artifact(),
    )
    observed = ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
        case_id="migration-answer-strategy-transfer-001",
    )
    assert not observed.passed
    assert observed.observed_facts["baseline_score"] == 4
    assert observed.observed_facts["candidate_score"] == 0
    assert "assertion_failed:evolution.positive_or_nonnegative_transfer" in observed.failure_tags


def test_migration_refuses_unbound_baseline(tmp_path: Path) -> None:
    subject = EvaluationSubject.from_artifact(
        candidate_id="no-baseline", target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    with pytest.raises(ValueError, match="bound baseline"):
        ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
            subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
            case_id="migration-answer-strategy-transfer-001",
        )


def test_structured_label_cannot_hide_actual_rag_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incorrectly_enter_rag(self: QueryAnswerService, session: Any, query: str) -> Any:
        return self._rag.answer(session, query, route=QueryRoute.HYBRID)

    monkeypatch.setattr(QueryAnswerService, "answer", incorrectly_enter_rag)
    subject = EvaluationSubject.from_artifact(
        candidate_id="rag-entry-mutation", target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    observed = ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
        case_id="retention-structured-direct-lookup-001",
    )
    assert observed.observed_facts["selected_route"] == "structured"
    assert observed.observed_facts["answer_probe"]["rag_entry_calls"] == 1
    assert "assertion_failed:rag.agentic_not_used" in observed.failure_tags


def test_retention_uses_real_answer_dispatch_and_never_enters_rag(tmp_path: Path) -> None:
    subject = EvaluationSubject.from_artifact(
        candidate_id="real-retention", target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    observed = ProtectedFixedSuiteRunner(project_root=Path.cwd()).run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path,
        case_id="retention-structured-direct-lookup-001",
    )
    assert observed.passed
    probe = observed.observed_facts["answer_probe"]
    assert probe["query_dispatch_count"] == 2
    assert probe["rag_entry_calls"] == 0
    assert probe["serving_release_available"] is True
    assert not probe["candidate_answer_rows"]


def test_candidate_gate_requires_real_formal_recommendation_positive_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = EvaluationSubject.from_artifact(
        candidate_id="recommendation-positive-control",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    runner = ProtectedFixedSuiteRunner(project_root=Path.cwd())
    healthy = runner.run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path / "healthy",
        case_id="safety-candidate-false-activation-001",
    )
    assert healthy.passed
    assert healthy.observed_facts["recommendation_probe"]["visible_recommendation_count"] == 1
    monkeypatch.setattr(KnowledgeGapService, "recommend", lambda *args, **kwargs: ())
    broken = runner.run_case(
        subject, stage=G006ExecutionStage.VALIDATION, work_dir=tmp_path / "broken",
        case_id="safety-candidate-false-activation-001",
    )
    assert "assertion_failed:memory.candidate_false_activation_zero" in broken.failure_tags
    assert not broken.observed_facts["recommendation_outcomes"][
        "recommendation.formal_goal_visible"
    ]
