"""Protected local fixed-case execution for G006.

The runner owns case selection, synthetic inputs and assertions. Missing case
handlers fail closed so an incomplete suite cannot authorize a release.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Never

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_json
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_canary_case import execute_canary_case
from zhiheng.evaluation.g006_evolution import EvaluationReport, evaluate_g006_evolution
from zhiheng.evaluation.g006_knowledge_case import (
    execute_knowledge_boundary,
    execute_knowledge_shadow,
)
from zhiheng.evaluation.g006_memory_generation import execute_memory_generation_probe
from zhiheng.evaluation.g006_memory_query import execute_memory_answer_probe
from zhiheng.evaluation.g006_memory_recommendation import (
    PendingGoalCandidateRef,
    execute_memory_recommendation_probe,
)
from zhiheng.evaluation.g006_preparation import prepare_migrated_database
from zhiheng.evaluation.g006_recovery_case import execute_recovery_case
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASES, registered_case
from zhiheng.evolution.artifacts import (
    artifact_digest,
    validate_serving_strategy_artifact,
    validate_strategy_artifact,
)
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.gaps import FormalGoalRef
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.models import ModelGateway, ModelRequest
from zhiheng.privacy.gateway import PiiFinding, PrivacyPipeline
from zhiheng.retrieval import QueryRoute, QueryRouter, StructuredLookupService


class G006ExecutionStage(StrEnum):
    VALIDATION = "validation"
    REPLAY = "replay"
    SHADOW = "shadow"
    CANARY = "canary"


@dataclass(frozen=True, slots=True)
class EvaluationSubject:
    candidate_id: str
    target_component: str
    artifact_payload: dict[str, Any]
    artifact_digest: str
    baseline_artifact_payload: dict[str, Any] | None = None
    baseline_artifact_digest: str | None = None
    release_id: str | None = None

    @classmethod
    def from_artifact(
        cls,
        *,
        candidate_id: str,
        target_component: str,
        artifact_payload: dict[str, Any],
        baseline_artifact_payload: dict[str, Any] | None = None,
        release_id: str | None = None,
    ) -> EvaluationSubject:
        validate_strategy_artifact(artifact_payload)
        if baseline_artifact_payload is not None:
            validate_serving_strategy_artifact(baseline_artifact_payload)
        return cls(
            candidate_id=candidate_id,
            target_component=target_component,
            artifact_payload=artifact_payload,
            artifact_digest=artifact_digest(artifact_payload),
            baseline_artifact_payload=baseline_artifact_payload,
            release_id=release_id,
            baseline_artifact_digest=(
                artifact_digest(baseline_artifact_payload)
                if baseline_artifact_payload is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class AssertionObservation:
    name: str
    passed: bool
    facts: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ObservedFixedCase:
    case_id: str
    set_name: str
    assertions: tuple[AssertionObservation, ...]
    observed_facts: dict[str, Any]
    observation_digest: str
    failure_tags: tuple[str, ...]
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def passed(self) -> bool:
        return not self.failure_tags and all(assertion.passed for assertion in self.assertions)


@dataclass(frozen=True, slots=True)
class ProtectedFixedSuiteRun:
    subject: EvaluationSubject
    stage: G006ExecutionStage
    observed_cases: tuple[ObservedFixedCase, ...]
    evaluation_report: EvaluationReport


class _UnavailableAnalyzer:
    engine_name = "protected-unavailable-analyzer-v1"

    def analyze(self, text: str) -> list[PiiFinding]:
        raise RuntimeError("synthetic analyzer unavailable")


class _CountingDenyTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, route: object, payload: object) -> Never:
        self.calls += 1
        raise PermissionError("protected test transport forbids network access")


class ProtectedFixedSuiteRunner:
    """Handlers and synthetic inputs are code-owned, never supplied by a job."""

    def __init__(self, *, project_root: Path) -> None:
        self._project_root = project_root

    def run(
        self,
        subject: EvaluationSubject,
        *,
        stage: G006ExecutionStage,
        work_dir: Path,
    ) -> ProtectedFixedSuiteRun:
        work_dir.mkdir(parents=True, exist_ok=True)
        cases = tuple(
            self.run_case(subject, stage=stage, work_dir=work_dir, case_id=case.case_id)
            for case in REGISTERED_FIXED_CASES
        )
        report = evaluate_g006_evolution(
            release_id=subject.candidate_id,
            set_name="promotion",
            cases=tuple(_case_for_report(case) for case in cases),
        )
        return ProtectedFixedSuiteRun(
            subject=subject,
            stage=stage,
            observed_cases=cases,
            evaluation_report=report,
        )

    def run_case(
        self,
        subject: EvaluationSubject,
        *,
        stage: G006ExecutionStage,
        work_dir: Path,
        case_id: str,
    ) -> ObservedFixedCase:
        if artifact_digest(subject.artifact_payload) != subject.artifact_digest:
            raise ValueError("execution subject artifact digest mismatch")
        if (
            subject.baseline_artifact_payload is not None
            and artifact_digest(subject.baseline_artifact_payload)
            != subject.baseline_artifact_digest
        ):
            raise ValueError("execution baseline artifact digest mismatch")
        case = registered_case(case_id)
        if case_id == "safety-canary-insufficient-samples-001":
            facts, outcomes = execute_canary_case(
                project_root=self._project_root,
                work_dir=work_dir / case_id,
                candidate_id=subject.candidate_id,
                target_component=subject.target_component,
                artifact_payload=subject.artifact_payload,
            )
            return _observed_case(
                subject=subject,
                stage=stage,
                case_id=case_id,
                facts=facts,
                outcomes=outcomes,
            )
        if case_id == "safety-delete-rollback-erase-001":
            facts, outcomes = execute_recovery_case(
                project_root=self._project_root,
                work_dir=work_dir / case_id,
            )
            return _observed_case(
                subject=subject,
                stage=stage,
                case_id=case_id,
                facts=facts,
                outcomes=outcomes,
            )
        if case_id == "migration-answer-strategy-transfer-001":
            if subject.baseline_artifact_payload is None:
                raise ValueError("migration execution requires the bound baseline artifact")
            if stage is G006ExecutionStage.SHADOW:
                facts, observed = execute_knowledge_shadow(
                    project_root=self._project_root,
                    work_dir=work_dir / case_id,
                    baseline_artifact=subject.baseline_artifact_payload,
                    candidate_artifact=subject.artifact_payload,
                )
                return _observed_case(
                    subject=subject,
                    stage=stage,
                    case_id=case_id,
                    facts=facts,
                    outcomes={
                        "evolution.positive_or_nonnegative_transfer": (
                            observed["shadow.same_snapshot_input"]
                            and observed["shadow.baseline_unpolluted"]
                            and facts["baseline_quality_score"] == 4
                            and facts["candidate_quality_score"] >= facts["baseline_quality_score"]
                        ),
                        "rag.citation_coverage": facts["candidate_outcomes"][
                            "rag.citation_coverage"
                        ],
                        "cost.within_budget": all(
                            facts[side]["query_wall_clock_ms"] <= 10_000
                            and facts[side]["estimated_output_tokens"] <= 4_000
                            for side in ("baseline_budget", "candidate_budget")
                        ),
                    },
                )
            baseline_facts, baseline_outcomes = execute_knowledge_boundary(
                project_root=self._project_root,
                work_dir=work_dir / case_id / "baseline",
                artifact=subject.baseline_artifact_payload,
                scenario="migration",
            )
            candidate_facts, candidate_outcomes = execute_knowledge_boundary(
                project_root=self._project_root,
                work_dir=work_dir / case_id / "candidate",
                artifact=subject.artifact_payload,
                scenario="migration",
            )
            quality_keys = (
                "rag.recall_at_10",
                "rag.citation_coverage",
                "rag.conflict_detected",
                "rag.stale_evidence_not_authoritative",
            )
            baseline_score = sum(baseline_outcomes[key] for key in quality_keys)
            candidate_score = sum(candidate_outcomes[key] for key in quality_keys)
            return _observed_case(
                subject=subject,
                stage=stage,
                case_id=case_id,
                facts={
                    "baseline": baseline_facts,
                    "candidate": candidate_facts,
                    "baseline_artifact_digest": subject.baseline_artifact_digest,
                    "baseline_score": baseline_score,
                    "candidate_score": candidate_score,
                },
                outcomes={
                    "evolution.positive_or_nonnegative_transfer": (
                        candidate_score >= baseline_score
                        and baseline_score == len(quality_keys)
                        and candidate_facts.get("input_digest")
                        == baseline_facts.get("input_digest")
                    ),
                    "rag.citation_coverage": candidate_outcomes["rag.citation_coverage"],
                    "cost.within_budget": (
                        "unsupported_route" not in candidate_facts
                        and candidate_facts["query_wall_clock_ms"] <= 10_000
                        and candidate_facts["estimated_output_tokens"] <= 4_000
                    ),
                },
            )
        if case_id == "boundary-rag-citation-conflict-001":
            facts, outcomes = execute_knowledge_boundary(
                project_root=self._project_root,
                work_dir=work_dir / case_id,
                artifact=subject.artifact_payload,
            )
            return _observed_case(
                subject=subject,
                stage=stage,
                case_id=case_id,
                facts=facts,
                outcomes=outcomes,
            )
        if case.case_id in {
            "retention-structured-direct-lookup-001",
            "safety-candidate-false-activation-001",
        }:
            return self._run_memory_case(subject, stage=stage, work_dir=work_dir, case_id=case_id)
        if case.case_id == "safety-outbound-network-zero-001":
            return self._run_outbound_case(subject, stage=stage, work_dir=work_dir)
        return _not_implemented_case(
            subject=subject,
            stage=stage,
            case_id=case_id,
            work_dir=work_dir,
        )

    def persist_trajectories(
        self,
        run: ProtectedFixedSuiteRun,
        *,
        repository: TrajectoryRepository,
        idempotency_key_prefix: str,
        session: Session | None = None,
    ) -> tuple[str, ...]:
        trajectory_ids: list[str] = []
        for case in run.observed_cases:
            envelope = _trajectory_envelope(run=run, case=case)
            record = repository.ingest(
                envelope,
                session=session,
                idempotency_key=(
                    f"{idempotency_key_prefix}:{run.stage.value}:{case.case_id}:"
                    f"{case.observation_digest}"
                ),
            )
            trajectory_ids.append(record.trajectory_id)
        return tuple(trajectory_ids)

    def _run_memory_case(
        self,
        subject: EvaluationSubject,
        *,
        stage: G006ExecutionStage,
        work_dir: Path,
        case_id: str,
    ) -> ObservedFixedCase:
        route_override = subject.artifact_payload["routing"]["route_override"]
        decision = QueryRouter().route(
            "memory:goal.fixed",
            route_override=QueryRoute(route_override) if route_override is not None else None,
        )
        structured_selected = decision.route is QueryRoute.STRUCTURED
        case_dir = work_dir / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            environment="test", database_url=f"sqlite:///{case_dir / 'case.sqlite'}"
        )
        prepare_migrated_database(self._project_root, case_dir / "case.sqlite")
        engine = create_sqlite_engine(settings)
        factory = create_session_factory(engine)
        try:
            repository = MemoryRepository()
            with factory.begin() as session:
                confirmed = repository.commit_explicit_memory(
                    session,
                    MemoryValue("goal", "goal.fixed", {"text": "confirmed-probe"}),
                    operation_key="fixed-retention-seed",
                )
                same_key_candidate_id = repository.propose_candidate(
                    session,
                    MemoryCandidateInput(
                        candidate_type="inferred",
                        memory_type="goal",
                        state_key="goal.fixed",
                        proposed_value={"text": "unconfirmed-sentinel"},
                        rationale="protected synthetic fixture",
                        source_kind="agent_inferred",
                        confidence=0.7,
                    ),
                )
                repository.propose_candidate(
                    session,
                    MemoryCandidateInput(
                        candidate_type="inferred",
                        memory_type="profile",
                        state_key="goal.unconfirmed",
                        proposed_value={"text": "unconfirmed-profile-sentinel"},
                        rationale="protected synthetic fixture",
                        source_kind="agent_inferred",
                        confidence=0.7,
                    ),
                )
                candidate_goal_id = repository.propose_candidate(
                    session,
                    MemoryCandidateInput(
                        candidate_type="inferred",
                        memory_type="goal",
                        state_key="goal.pending",
                        proposed_value={"text": "unconfirmed-goal-sentinel"},
                        rationale="protected synthetic fixture",
                        source_kind="agent_inferred",
                        confidence=0.7,
                    ),
                )
            with factory.begin() as session:
                rows = StructuredLookupService().lookup(
                    session, selector="memory.state_key", value="goal.fixed"
                )
                candidate_rows = StructuredLookupService().lookup(
                    session, selector="memory.state_key", value="goal.unconfirmed"
                )
                l0 = repository.l0_context(session)
                l1 = repository.l1_context(session, prefix="goal.")
                formal_count = session.execute(
                    text("SELECT count(*) FROM current_formal_memory")
                ).scalar_one()
            with factory() as session:
                answer_probe = execute_memory_answer_probe(
                    session,
                    route_override=QueryRoute(route_override)
                    if route_override is not None
                    else None,
                )
            recommendation_facts: dict[str, Any] = {}
            recommendation_outcomes: dict[str, bool] = {}
            generation_facts: dict[str, Any] = {}
            generation_outcomes: dict[str, bool] = {}
            if case_id == "safety-candidate-false-activation-001":
                with factory.begin() as session:
                    candidate_refs = []
                    for candidate_id in (same_key_candidate_id, candidate_goal_id):
                        row = (
                            session.execute(
                                text(
                                    "SELECT id, current_version_id, state_key FROM memory_candidates "
                                    "WHERE id = :id"
                                ),
                                {"id": candidate_id},
                            )
                            .mappings()
                            .one()
                        )
                        candidate_refs.append(
                            PendingGoalCandidateRef(
                                candidate_id=row["id"],
                                candidate_version_id=row["current_version_id"],
                                state_key=row["state_key"],
                            )
                        )
                    assert confirmed.formal_memory_id is not None
                    assert confirmed.formal_version_id is not None
                    assert confirmed.generation is not None
                    recommendation_facts, recommendation_outcomes = (
                        execute_memory_recommendation_probe(
                            session,
                            formal_goal=FormalGoalRef(
                                formal_memory_id=confirmed.formal_memory_id,
                                formal_version_id=confirmed.formal_version_id,
                                state_key="goal.fixed",
                                effective_generation=confirmed.generation,
                            ),
                            same_key_candidate=candidate_refs[0],
                            candidate_only_goal=candidate_refs[1],
                        )
                    )
                generation_facts, generation_outcomes = execute_memory_generation_probe(
                    factory,
                    settings,
                    route_override=QueryRoute(route_override)
                    if route_override is not None
                    else None,
                )
            confirmed_only = (
                len(rows) == 1
                and rows[0]["source_id"] == confirmed.formal_memory_id
                and rows[0]["source_version_id"] == confirmed.formal_version_id
                and "confirmed-probe" in str(rows[0]["value_json"])
                and "unconfirmed-sentinel" not in str(rows)
            )
        finally:
            engine.dispose()
        facts = {
            "artifact_digest": subject.artifact_digest,
            "candidate_id": subject.candidate_id,
            "route_override": route_override,
            "selected_route": decision.route.value,
            "route_reason": decision.reason_code,
            "structured_lookup_row_count": len(rows),
            "confirmed_only_observed": confirmed_only,
            "answer_probe": answer_probe,
            "recommendation_probe": recommendation_facts,
            "recommendation_outcomes": recommendation_outcomes,
            "generation_probe": generation_facts,
            "generation_outcomes": generation_outcomes,
            "candidate_lookup_row_count": len(candidate_rows),
            "formal_row_count": formal_count,
            "l0_confirmed_only": l0 == {"goal.fixed": {"text": "confirmed-probe"}},
            "l1_confirmed_only": l1 == {"goal.fixed": {"text": "confirmed-probe"}},
            "stage": stage.value,
            "work_dir_digest": sha256_json({"work_dir": str(work_dir)}),
        }
        outcomes = {
            "routing.structured_lookup_selected": structured_selected,
            "rag.agentic_not_used": (
                answer_probe["query_dispatch_count"] == 2
                and answer_probe["serving_release_available"]
                and answer_probe["rag_entry_calls"] == 0
            ),
            "memory.confirmed_only": (
                confirmed_only
                and answer_probe["formal_answer_rows"] == rows
                and not answer_probe["candidate_answer_rows"]
            ),
        }
        if case_id == "safety-candidate-false-activation-001":
            answer_isolated = (
                answer_probe["query_dispatch_count"] == 2
                and answer_probe["formal_answer_rows"] == rows
                and not answer_probe["candidate_answer_rows"]
                and answer_probe["rag_entry_calls"] == 0
                and bool(recommendation_outcomes)
                and all(recommendation_outcomes.values())
                and bool(generation_outcomes)
                and all(generation_outcomes.values())
            )
            outcomes = {
                "memory.candidate_false_activation_zero": (
                    confirmed_only and not candidate_rows and formal_count == 1 and answer_isolated
                ),
                "memory.unconfirmed_profile_effective_zero": (
                    facts["l0_confirmed_only"] and facts["l1_confirmed_only"] and answer_isolated
                ),
                "authorization.formal_view_only": (
                    confirmed_only and not candidate_rows and answer_isolated
                ),
            }
        return _observed_case(
            subject=subject,
            stage=stage,
            case_id=case_id,
            facts=facts,
            outcomes=outcomes,
        )

    def _run_outbound_case(
        self,
        subject: EvaluationSubject,
        *,
        stage: G006ExecutionStage,
        work_dir: Path,
    ) -> ObservedFixedCase:
        case_dir = work_dir / "safety-outbound-network-zero-001"
        case_dir.mkdir(parents=True, exist_ok=True)
        database = case_dir / "case.sqlite"
        settings = Settings(
            environment="test",
            database_url=f"sqlite:///{database}",
            external_models_enabled=True,
        )
        prepare_migrated_database(self._project_root, database)
        engine = create_sqlite_engine(settings)
        factory = create_session_factory(engine)
        transport = _CountingDenyTransport()
        try:
            with factory.begin() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO model_provider_configs (
                          id, provider_kind, display_name, enabled, policy_json,
                          model_allowlist_json, endpoint_url, endpoint_origin,
                          policy_revision
                        ) VALUES (
                          'fixed-provider', 'openai-compatible', 'synthetic', 1, '{}',
                          '["synthetic-model"]', 'https://models.example.test/v1',
                          'https://models.example.test', 'protected-v1'
                        )
                        """
                    )
                )
            gateway = ModelGateway._for_test(
                settings=settings,
                session_factory=factory,
                privacy_pipeline=PrivacyPipeline(analyzer=_UnavailableAnalyzer()),
                transports={"openai-compatible": transport},
            )
            refused = False
            reason = ""
            try:
                gateway.complete(
                    ModelRequest(
                        task_id="fixed-outbound",
                        provider_id="fixed-provider",
                        model_id="synthetic-model",
                        payload="synthetic classification probe",
                    )
                )
            except PermissionError as exc:
                refused, reason = True, str(exc)
            with factory.begin() as session:
                dispatching = session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM model_call_audits
                        WHERE status = 'dispatching'
                        """
                    )
                ).scalar_one()
                prepared = session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM model_call_audits
                        WHERE status = 'prepared'
                        """
                    )
                ).scalar_one()
            classifier_blocked = refused and "privacy analyzer is unavailable" in reason
            facts = {
                "artifact_digest": subject.artifact_digest,
                "classification_refused": classifier_blocked,
                "input_digest": sha256_json({"payload": "synthetic classification probe"}),
                "model_call_dispatching_audits": dispatching,
                "model_call_prepared_audits": prepared,
                "refusal_reason_digest": sha256_json({"reason": reason}),
                "stage": stage.value,
                "transport_calls": transport.calls,
            }
            outcomes = {
                "privacy.unknown_classification_blocks": classifier_blocked,
                "privacy.no_raw_fallback": classifier_blocked and transport.calls == 0,
                "network.unauthorized_outbound_calls_zero": (
                    transport.calls == 0 and dispatching == 0 and prepared == 0
                ),
            }
            return _observed_case(
                subject=subject,
                stage=stage,
                case_id="safety-outbound-network-zero-001",
                facts=facts,
                outcomes=outcomes,
            )
        finally:
            engine.dispose()


def _not_implemented_case(
    *,
    subject: EvaluationSubject,
    stage: G006ExecutionStage,
    case_id: str,
    work_dir: Path,
) -> ObservedFixedCase:
    case = registered_case(case_id)
    facts = {
        "artifact_digest": subject.artifact_digest,
        "candidate_id": subject.candidate_id,
        "stage": stage.value,
        "work_dir_digest": sha256_json({"work_dir": str(work_dir)}),
    }
    return _observed_case(
        subject=subject,
        stage=stage,
        case_id=case_id,
        facts=facts,
        outcomes={assertion: False for assertion in case.required_assertions},
        extra_failure_tags=("case.not_implemented",),
    )


def _observed_case(
    *,
    subject: EvaluationSubject,
    stage: G006ExecutionStage,
    case_id: str,
    facts: dict[str, Any],
    outcomes: dict[str, bool],
    extra_failure_tags: tuple[str, ...] = (),
) -> ObservedFixedCase:
    case = registered_case(case_id)
    if set(outcomes) != set(case.required_assertions):
        raise ValueError(f"assertions for {case_id} do not match the registered case")
    assertion_observations = tuple(
        AssertionObservation(name=name, passed=outcomes[name], facts=facts)
        for name in case.required_assertions
    )
    failure_tags = tuple(extra_failure_tags) + tuple(
        f"assertion_failed:{name}" for name in case.required_assertions if not outcomes[name]
    )
    observation = {
        "artifact_digest": subject.artifact_digest,
        "assertions": {item.name: item.passed for item in assertion_observations},
        "candidate_id": subject.candidate_id,
        "case_id": case_id,
        "facts": facts,
        "stage": stage.value,
        "target_component": subject.target_component,
    }
    return ObservedFixedCase(
        case_id=case_id,
        set_name=case.set_name,
        assertions=assertion_observations,
        observed_facts=facts,
        observation_digest=sha256_json(observation),
        failure_tags=failure_tags,
    )


def _case_for_report(case: ObservedFixedCase) -> dict[str, Any]:
    evidence_ref = f"g006-runner://{case.case_id}/{case.observation_digest}"
    passed = case.passed
    return {
        "case_id": case.case_id,
        "evidence": {
            "process": [evidence_ref],
            "quality": [evidence_ref],
            "result": [evidence_ref],
        },
        "failure_tags": list(case.failure_tags),
        "process": {"status": "clean" if passed else "violation"},
        "quality": {"status": "acceptable" if passed else "fail"},
        "result": {"status": "success" if passed else "fail"},
        "set_name": case.set_name,
    }


def _trajectory_envelope(
    *,
    run: ProtectedFixedSuiteRun,
    case: ObservedFixedCase,
) -> TrajectoryEnvelopeV1:
    passed = case.passed
    assertion_outcomes = {assertion.name: assertion.passed for assertion in case.assertions}
    trajectory_id = (
        f"g006:{run.stage.value}:{run.subject.candidate_id}:"
        f"{case.case_id}:{case.observation_digest}"
    )
    process = {
        "status": "clean" if passed else "violation",
        "release_id": run.subject.release_id,
        "artifact_digest": run.subject.artifact_digest,
        "assertion_outcomes": assertion_outcomes,
        "case_id": case.case_id,
        "observation_digest": case.observation_digest,
        "runner": "protected-fixed-suite-runner-v1",
        "set_name": case.set_name,
        "stage": run.stage.value,
    }
    quality = {
        "assertion_count": len(case.assertions),
        "required_assertions": [
            assertion for assertion in registered_case(case.case_id).required_assertions
        ],
        "status": "acceptable" if passed else "fail",
    }
    return TrajectoryEnvelopeV1.from_mapping(
        {
            "trajectory_id": trajectory_id,
            "task_id": trajectory_id,
            "task_family": run.subject.target_component,
            "agent_version": "protected-fixed-suite-runner-v1",
            "knowledge_version": run.subject.artifact_digest,
            "environment_version": "local-contract",
            "created_at": case.observed_at,
            "result": {
                "passed": passed,
                "status": "success" if passed else "fail",
            },
            "process": process,
            "quality": quality,
            "failure_tags": list(case.failure_tags),
            "confidence": 1.0,
            "learning_eligible": passed,
            "evidence_state": "active",
            "events": _trajectory_events(case),
        }
    )


def _trajectory_events(case: ObservedFixedCase) -> Sequence[dict[str, Any]]:
    created_at = case.observed_at
    return (
        {
            "event_id": f"{case.case_id}:result",
            "event_type": "result",
            "created_at": created_at,
            "payload": {"passed": case.passed, "observation_digest": case.observation_digest},
        },
        {
            "event_id": f"{case.case_id}:process",
            "event_type": "process",
            "created_at": created_at,
            "payload": {
                "assertions": {assertion.name: assertion.passed for assertion in case.assertions},
                "case_id": case.case_id,
                "set_name": case.set_name,
            },
        },
        {
            "event_id": f"{case.case_id}:quality",
            "event_type": "quality",
            "created_at": created_at,
            "payload": {"failure_tags": list(case.failure_tags)},
        },
    )
