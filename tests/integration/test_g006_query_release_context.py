from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.release_helpers import (
    advance_to_canary_with_execution,
    prepare_release_with_persisted_evidence,
)
from tests.integration.release_helpers import (
    insert_signed_canary_observations as _insert_canary_observations,
)
from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import (
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseContext, ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query import AgenticBudget, AnswerClaim, BoundedAgenticRagService, GeneratedAnswer
from zhiheng.query.contracts import (
    AnswerEnvelope,
    BudgetUsage,
    ReleaseBehaviorConfig,
    ReleasePreview,
    StopReason,
)
from zhiheng.query.service import QueryAnswerService
from zhiheng.retrieval import QueryRoute, QueryRouter
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    Citation,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalSource,
)
from zhiheng.retrieval.hybrid import HybridRetriever

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_COMPONENT = "retrieval.answer_strategy"
FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(REPO_ROOT / "src"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    command.upgrade(_alembic_config(db_path), "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _sqlite(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _artifact_payload(
    *,
    rrf_k: int | None = 60,
    overfetch_factor: int = 4,
    route_override: str | None = None,
) -> dict[str, Any]:
    return {
        "retrieval": {
            "rrf_k": rrf_k,
            "overfetch_factor": overfetch_factor,
        },
        "routing": {"route_override": route_override},
    }


def _digest(label: str) -> str:
    del label
    return artifact_digest(default_release_artifact())


def _binding_with_artifact(
    candidate_id: str,
    rollback_target_id: str,
    artifact_payload: dict[str, Any],
) -> ReleaseBindingV1:
    binding = _binding(candidate_id, rollback_target_id)
    return replace(binding, approved_artifact_digest=artifact_digest(artifact_payload))


def _assignment(cohort: str) -> CanaryAssignment:
    return CanaryAssignment(
        scope={"cohort": cohort, "percentage": 5},
        expires_at="2026-10-01T00:00:00+00:00",
    )


def _binding(candidate_id: str, rollback_target_id: str) -> ReleaseBindingV1:
    return ReleaseBindingV1(
        candidate_id=candidate_id,
        target_component=TARGET_COMPONENT,
        source_evaluation_ids=FIXED_EVAL_SETS,
        source_evidence_refs=(f"synthetic://evidence/{candidate_id}",),
        validation_report_ref=f"synthetic://validation/{candidate_id}",
        reviewer_decision_ref=f"synthetic://review/{candidate_id}",
        approved_artifact_digest=_digest(candidate_id),
        rollback_target_id=rollback_target_id,
    )


def _drop_artifact_immutability_triggers(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_approved_immutable_delete")
    connection.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_approved_immutable_update")


def _create_artifact_immutability_triggers(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_evolution_artifacts_approved_immutable_update
        BEFORE UPDATE ON evolution_artifacts
        FOR EACH ROW
        WHEN OLD.status IN ('approved', 'published')
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifacts are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_evolution_artifacts_approved_immutable_delete
        BEFORE DELETE ON evolution_artifacts
        FOR EACH ROW
        WHEN OLD.status IN ('approved', 'published')
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifacts are immutable');
        END
        """
    )


def _with_artifact_mutation(
    connection: sqlite3.Connection,
    mutation: Callable[[], object],
) -> None:
    _drop_artifact_immutability_triggers(connection)
    try:
        mutation()
        connection.commit()
    finally:
        _create_artifact_immutability_triggers(connection)
        connection.commit()


def _user_approval_context(actor_id: str = "user-approver-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.USER_APPROVER)


def _publisher_context(actor_id: str = "publisher-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.PUBLISHER)


def _insert_artifact(
    connection: sqlite3.Connection,
    release: ReleaseContext,
    *,
    rrf_k: int = 60,
    overfetch_factor: int = 4,
    route_override: str | None = None,
    artifact_json: Any | None = None,
    created_at: str | None = None,
) -> None:
    payload = (
        artifact_json
        if artifact_json is not None
        else _artifact_payload(
            rrf_k=rrf_k,
            overfetch_factor=overfetch_factor,
            route_override=route_override,
        )
    )
    artifact_payload = json.dumps(payload)
    if created_at is None:
        connection.execute(
            """
            INSERT INTO evolution_artifacts (
              id, artifact_kind, binding_digest, artifact_digest, artifact_json, status, source_ref
            )
            VALUES (?, 'retrieval_strategy', ?, ?, ?, 'approved', ?)
            """,
            (
                new_id(),
                release.binding_digest,
                artifact_digest(payload)
                if isinstance(payload, dict)
                else release.binding.approved_artifact_digest,
                artifact_payload,
                f"synthetic://release-artifact/{release.release_id}",
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO evolution_artifacts (
              id, artifact_kind, binding_digest, artifact_digest, artifact_json, status,
              source_ref, created_at, updated_at
            )
            VALUES (?, 'retrieval_strategy', ?, ?, ?, 'approved', ?, ?, ?)
            """,
            (
                new_id(),
                release.binding_digest,
                artifact_digest(payload)
                if isinstance(payload, dict)
                else release.binding.approved_artifact_digest,
                artifact_payload,
                f"synthetic://release-artifact/{release.release_id}",
                created_at,
                created_at,
            ),
        )
    connection.commit()


def _advance_to_canary(
    controller: ReleaseController,
    release_id: str,
    *,
    actor_id: str = "publisher-a",
) -> ReleaseContext:
    return advance_to_canary_with_execution(controller, release_id, actor_id=actor_id)


def _bootstrap(
    db_path: Path,
    *,
    rrf_k: int = 60,
    overfetch_factor: int = 4,
) -> ReleaseContext:
    with _sqlite(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        stable = controller.load_default_head(TARGET_COMPONENT)
        assert stable is not None
        _insert_artifact(connection, stable, rrf_k=rrf_k, overfetch_factor=overfetch_factor)
        return stable


class _RecordingRag:
    def __init__(self) -> None:
        self.calls: list[tuple[ReleaseContext | None, ReleaseBehaviorConfig]] = []

    def answer(
        self,
        session: Session,
        query: str,
        *,
        route: QueryRoute,
        release_context: ReleaseContext | None = None,
        behavior: ReleaseBehaviorConfig | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> AnswerEnvelope:
        del memory_context, conversation_context
        self.calls.append((release_context, behavior or ReleaseBehaviorConfig()))
        return AnswerEnvelope(
            answer="ok",
            claims=(),
            citations=(),
            conflicts=(),
            assumptions=(),
            insufficiencies=(),
            route=route,
            stop_reason=StopReason.COMPLETED,
            budget_usage=BudgetUsage(),
            release_context=release_context,
        )


class _Model:
    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> GeneratedAnswer:
        del query, manifest, max_output_tokens, memory_context
        return GeneratedAnswer(
            answer="answer from authorized evidence",
            claims=(AnswerClaim(text="evidence", citation_ids=(citations[0].citation_id,)),)
            if citations
            else (),
        )


class _TransactionCheckingModel:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.calls = 0

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> GeneratedAnswer:
        del query, manifest, max_output_tokens, memory_context
        self.calls += 1
        assert not self._session.in_transaction()
        return GeneratedAnswer(
            answer="answer from authorized evidence",
            claims=(AnswerClaim(text="evidence", citation_ids=(citations[0].citation_id,)),)
            if citations
            else (),
        )


class _Verifier:
    def validate_manifest(self, session: Session, manifest: AuthorizedContextManifest) -> bool:
        return True


class _Lexical:
    def __init__(self, candidate: RetrievalCandidate) -> None:
        self._candidate = candidate
        self.limits: list[int] = []

    def search(
        self, session: Session, query: str, *, limit: int, filters: RetrievalFilters | None = None
    ) -> list[RetrievalCandidate]:
        self.limits.append(limit)
        return [self._candidate]


class _Vector:
    def search(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        generation_id: str,
        limit: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalCandidate]:
        return []


class _Authorizer:
    def __init__(self, chunk: AuthorizedChunk) -> None:
        self._chunk = chunk

    def authorize_batch(
        self,
        session: Session,
        candidates: Sequence[RetrievalCandidate],
    ) -> list[AuthorizedChunk]:
        return [
            replace(self._chunk, score=candidate.score, rank=candidate.rank)
            for candidate in candidates
        ]

    def seal_manifest(
        self,
        *,
        query_hash: str,
        chunks: Sequence[AuthorizedChunk],
    ) -> AuthorizedContextManifest:
        return RetrievalAuthorizer().seal_manifest(query_hash=query_hash, chunks=chunks)

    def validate_manifest(self, session: Session, manifest: AuthorizedContextManifest) -> bool:
        return True


def _chunk() -> AuthorizedChunk:
    return AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id="chunk-1",
        confirmation_generation=1,
        generation=None,
        title="Formal",
        text="authorized evidence",
        span_start=0,
        span_end=len("authorized evidence"),
        content_version_id="content-1",
        content_span_id="span-1",
        evidence_object_id="evidence-1",
        page_no=None,
        section_path=None,
        quote_hash=sha256_text("authorized evidence"),
        score=1.0,
        rank=1,
        retrievers=(RetrievalSource.LEXICAL,),
    )


def _candidate() -> RetrievalCandidate:
    return RetrievalCandidate(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id="chunk-1",
        confirmation_generation=1,
        generation=None,
        rank=1,
        score=1.0,
        retriever=RetrievalSource.LEXICAL,
        component_ranks=((RetrievalSource.LEXICAL, 1),),
    )


def _service_with_real_hybrid(
    *,
    lexical: _Lexical,
    authorizer: _Authorizer,
    trajectory_repository: TrajectoryRepository | None = None,
) -> QueryAnswerService:
    hybrid = HybridRetriever(
        lexical=lexical,
        vector=_Vector(),
        authorizer=authorizer,  # type: ignore[arg-type]
    )
    return QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=object(),  # type: ignore[arg-type]
        rag=BoundedAgenticRagService(
            structured_lookup=object(),  # type: ignore[arg-type]
            hybrid_retrieval=hybrid,
            evidence_verifier=_Verifier(),
            model_gateway=_Model(),
            budget=AgenticBudget(max_context_chunks=2),
        ),
        trajectory_repository=trajectory_repository,
    )


def test_query_service_serves_default_stable_and_isolates_candidate_and_canary(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    with _sqlite(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        artifact_payload = _artifact_payload(rrf_k=10)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding_with_artifact("candidate-v1", stable.release_id, artifact_payload),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("canary-v1"),
            canary_samples=5,
            request_id="prepare-candidate",
            artifact_payload=artifact_payload,
        )
        canary = _advance_to_canary(controller, prepared.release_id)

    rag = _RecordingRag()
    service = QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=object(),  # type: ignore[arg-type]
        rag=rag,  # type: ignore[arg-type]
    )
    with session_scope(session_factory) as session:
        default = service.answer(session, "hybrid query")
        blocked = service.answer(
            session,
            "hybrid query",
            release_preview=ReleasePreview(release_id=canary.release_id),
        )
        explicit = service.answer(
            session,
            "hybrid query",
            release_preview=ReleasePreview(
                release_id=canary.release_id,
                synthetic_worker=True,
                assignment_scope={"cohort": "canary-v1", "percentage": 5},
            ),
        )

    assert isinstance(default, AnswerEnvelope)
    assert default.release_context is not None
    assert default.release_context.release_id == stable.release_id
    assert isinstance(blocked, AnswerEnvelope)
    assert blocked.stop_reason is StopReason.RELEASE_UNAVAILABLE
    assert isinstance(explicit, AnswerEnvelope)
    assert explicit.release_context is not None
    assert explicit.release_context.release_id == canary.release_id
    assert [call[0].release_id for call in rag.calls if call[0] is not None] == [
        stable.release_id,
        canary.release_id,
    ]


def test_real_canary_query_observations_are_required_before_stable_promotion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    with _sqlite(db_path) as connection:
        controller = ReleaseController.from_db(
            connection, deployment_secret="test-deployment-secret"
        )
        artifact_payload = _artifact_payload(rrf_k=1, overfetch_factor=3)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding_with_artifact(
                "candidate-real-canary", stable.release_id, artifact_payload
            ),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("real-canary"),
            canary_samples=5,
            request_id="prepare-real-canary",
            artifact_payload=artifact_payload,
        )
        canary = _advance_to_canary(controller, prepared.release_id)

        with pytest.raises(ValueError, match="real append-only observations"):
            controller.promote_release(
                canary.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="promote-empty-canary",
            )

        connection.execute(
            """
            INSERT INTO task_trajectories (
              id, task_family, agent_version, knowledge_version, environment_version,
              status, evidence_refs_json
            )
            VALUES (?, 'release_canary', 'agent', 'knowledge', 'env', 'available', ?)
            """,
            (
                "forged-canary-observation",
                json.dumps(
                    {
                        "release_id": canary.release_id,
                        "binding_digest": canary.binding_digest,
                        "canary_cohort": "real-canary",
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO task_evaluations (
              id, trajectory_id, result_json, process_json, quality_json,
              failure_tags_json, confidence, learning_eligible
            )
            VALUES ('forged-canary-evaluation', 'forged-canary-observation',
                    '{}', '{}', '{}', '[]', 1.0, 1)
            """
        )
        connection.commit()

        with pytest.raises(ValueError, match="real append-only observations"):
            controller.promote_release(
                canary.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="promote-forged-canary",
            )

    lexical = _Lexical(_candidate())
    service = _service_with_real_hybrid(
        lexical=lexical,
        authorizer=_Authorizer(_chunk()),
        trajectory_repository=TrajectoryRepository(deployment_secret="test-deployment-secret"),
    )
    for index in range(5):
        with session_scope(session_factory) as session:
            result = service.answer(
                session,
                f"private raw query {index}",
                release_preview=ReleasePreview(
                    release_id=canary.release_id,
                    synthetic_worker=True,
                    assignment_scope={"cohort": "real-canary", "percentage": 5},
                ),
                idempotency_key=f"real-canary-{index}",
            )
            assert isinstance(result, AnswerEnvelope)
            assert result.release_context is not None
            assert result.release_context.release_id == canary.release_id

    with _sqlite(db_path) as connection:
        promoted = ReleaseController.from_db(
            connection, deployment_secret="test-deployment-secret"
        ).promote_release(
            canary.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-real-canary",
        )

    assert promoted.state is ReleaseState.STABLE
    assert promoted.rollback_target_id == stable.release_id


def test_query_uses_atomic_release_context_even_if_stable_head_changes_mid_request(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    with _sqlite(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        artifact_payload = _artifact_payload(rrf_k=1)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding_with_artifact("candidate-v1", stable.release_id, artifact_payload),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate",
            artifact_payload=artifact_payload,
        )
        _advance_to_canary(controller, prepared.release_id)
        _insert_canary_observations(
            connection,
            prepared.release_id,
            prepared.binding,
            cohort="candidate-v1",
        )

    class _MutatingRag(_RecordingRag):
        def answer(self, session: Session, query: str, **kwargs: Any) -> AnswerEnvelope:
            with _sqlite(db_path) as connection:
                ReleaseController.from_db(connection).promote_release(
                    prepared.release_id,
                    publisher_context=_publisher_context(),
                    user_approval_context=_user_approval_context(),
                    request_id=f"promote-{time.monotonic_ns()}",
                )
            return super().answer(session, query, **kwargs)

    rag = _MutatingRag()
    service = QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=object(),  # type: ignore[arg-type]
        rag=rag,  # type: ignore[arg-type]
    )
    with session_scope(session_factory) as session:
        result = service.answer(session, "hybrid query")

    assert isinstance(result, AnswerEnvelope)
    assert result.release_context is not None
    assert result.release_context.release_id == stable.release_id
    with _sqlite(db_path) as connection:
        default_head = ReleaseController.from_db(connection).load_default_head(TARGET_COMPONENT)
        assert default_head is not None
        assert default_head.release_id == prepared.release_id


def test_retrieval_runs_trajectory_and_behavior_follow_promotion_then_rollback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path, rrf_k=60, overfetch_factor=2)
    with _sqlite(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        artifact_payload = _artifact_payload(rrf_k=1, overfetch_factor=3)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=_binding_with_artifact("candidate-v1", stable.release_id, artifact_payload),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate",
            artifact_payload=artifact_payload,
        )
        _advance_to_canary(controller, prepared.release_id)
        _insert_canary_observations(
            connection,
            prepared.release_id,
            prepared.binding,
            cohort="candidate-v1",
        )

    lexical = _Lexical(_candidate())
    service = _service_with_real_hybrid(
        lexical=lexical,
        authorizer=_Authorizer(_chunk()),
        trajectory_repository=TrajectoryRepository(deployment_secret="test-deployment-secret"),
    )
    with session_scope(session_factory) as session:
        first = service.answer(session, "private raw query", idempotency_key="first")

    with _sqlite(db_path) as connection:
        promoted = ReleaseController.from_db(connection).promote_release(
            prepared.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-candidate",
        )

    with session_scope(session_factory) as session:
        second = service.answer(session, "private raw query", idempotency_key="second")

    with _sqlite(db_path) as connection:
        ReleaseController.from_db(connection).rollback_release(
            promoted.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="rollback-candidate",
        )

    with session_scope(session_factory) as session:
        third = service.answer(session, "private raw query", idempotency_key="third")
        rows = (
            session.execute(
                text(
                    """
                SELECT id, strategy_release_id
                FROM retrieval_runs
                ORDER BY rowid
                """
                )
            )
            .mappings()
            .all()
        )
        scores = (
            session.execute(
                text(
                    """
                SELECT score_json
                FROM retrieval_results
                ORDER BY rowid
                """
                )
            )
            .scalars()
            .all()
        )
        trajectory = (
            session.execute(
                text(
                    """
                SELECT tt.id, tt.evidence_refs_json, te.result_json,
                       te.process_json, te.quality_json
                FROM task_trajectories tt
                JOIN task_evaluations te ON te.trajectory_id = tt.id
                WHERE tt.task_family = :task_family
                  AND json_extract(te.process_json, '$.query_sha256') = :query_hash
                ORDER BY tt.rowid
                """
                ),
                {"task_family": TARGET_COMPONENT, "query_hash": sha256_text("private raw query")},
            )
            .mappings()
            .all()
        )

    assert isinstance(first, AnswerEnvelope)
    assert isinstance(second, AnswerEnvelope)
    assert isinstance(third, AnswerEnvelope)
    assert [row["strategy_release_id"] for row in rows] == [
        stable.release_id,
        promoted.release_id,
        stable.release_id,
    ]
    assert lexical.limits == [8, 6, 8]
    assert json.loads(scores[0])["score"] == 1 / 61
    assert json.loads(scores[1])["score"] == 1 / 2
    assert json.loads(scores[2])["score"] == 1 / 61
    assert first.retrieval_run_ids == (rows[0]["id"],)
    assert second.retrieval_run_ids == (rows[1]["id"],)
    assert third.retrieval_run_ids == (rows[2]["id"],)
    assert len(trajectory) == 3
    for row in trajectory:
        serialized = json.dumps(dict(row), ensure_ascii=False)
        assert "private raw query" not in serialized
        result_json = json.loads(row["result_json"])
        process_json = json.loads(row["process_json"])
        quality_json = json.loads(row["quality_json"])
        assert result_json["answer_sha256"] != "answer from authorized evidence"
        assert process_json["release_id"] in {stable.release_id, promoted.release_id}
        assert process_json["query_sha256"] == sha256_text("private raw query")
        assert quality_json["no_raw_query_or_model_output"] is True


def test_model_generation_runs_outside_sqlite_transaction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    lexical = _Lexical(_candidate())

    with session_scope(session_factory) as session:
        model = _TransactionCheckingModel(session)
        hybrid = HybridRetriever(
            lexical=lexical,
            vector=_Vector(),
            authorizer=_Authorizer(_chunk()),  # type: ignore[arg-type]
        )
        service = QueryAnswerService(
            router=QueryRouter(),
            structured_lookup=object(),  # type: ignore[arg-type]
            rag=BoundedAgenticRagService(
                structured_lookup=object(),  # type: ignore[arg-type]
                hybrid_retrieval=hybrid,
                evidence_verifier=_Verifier(),
                model_gateway=model,
                budget=AgenticBudget(max_context_chunks=2),
            ),
            trajectory_repository=TrajectoryRepository(deployment_secret="test-deployment-secret"),
        )
        result = service.answer(session, "private transaction query")

    assert isinstance(result, AnswerEnvelope)
    assert result.release_context is not None
    assert result.release_context.release_id == stable.release_id
    assert result.stop_reason is StopReason.COMPLETED
    assert model.calls == 1


def test_missing_release_artifact_returns_evidence_only_safe_mode_and_trajectory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    with _sqlite(db_path) as connection:
        missing_binding = replace(
            stable.binding,
            approved_artifact_digest=_digest("missing-artifact"),
        )
        connection.execute(
            """
            INSERT INTO release_transition_events (
              id, release_id, previous_state, next_state, actor_role, actor_id,
              binding_digest, reason, event_json, created_at, updated_at
            )
            VALUES (?, ?, 'prepared', 'stable', 'publisher', ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                stable.release_id,
                "missing-artifact-handler",
                missing_binding.canonical_digest(),
                "append_missing_artifact",
                json.dumps(
                    {
                        "binding": missing_binding.as_record(),
                        "request_id": "append-missing-artifact",
                        "step": "append_missing_artifact",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "2000-01-01 00:00:00",
                "2000-01-01 00:00:00",
            ),
        )
        connection.commit()

    service = QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=object(),  # type: ignore[arg-type]
        rag=_RecordingRag(),  # type: ignore[arg-type]
        trajectory_repository=TrajectoryRepository(deployment_secret="test-deployment-secret"),
    )
    with session_scope(session_factory) as session:
        result = service.answer(session, "private missing artifact query", idempotency_key="safe")
        trajectory = (
            session.execute(
                text(
                    """
                SELECT te.result_json, te.process_json
                FROM task_evaluations te
                ORDER BY te.created_at DESC
                LIMIT 1
                """
                )
            )
            .mappings()
            .one()
        )

    assert isinstance(result, AnswerEnvelope)
    assert result.stop_reason is StopReason.RELEASE_UNAVAILABLE
    assert "private missing artifact query" not in json.dumps(dict(trajectory), ensure_ascii=False)
    assert json.loads(trajectory["process_json"])["release_degraded_reasons"]


def test_corrupt_release_artifact_returns_safe_mode_without_calling_rag(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    stable = _bootstrap(db_path)
    with _sqlite(db_path) as connection:
        _with_artifact_mutation(
            connection,
            lambda: connection.execute(
                """
                UPDATE evolution_artifacts
                SET artifact_json = ?
                WHERE binding_digest = ?
                  AND artifact_digest = ?
                  AND artifact_kind = 'retrieval_strategy'
                  AND status IN ('approved', 'published')
                """,
                (json.dumps([]), stable.binding_digest, stable.binding.approved_artifact_digest),
            ),
        )

    rag = _RecordingRag()
    service = QueryAnswerService(
        router=QueryRouter(),
        structured_lookup=object(),  # type: ignore[arg-type]
        rag=rag,  # type: ignore[arg-type]
        trajectory_repository=TrajectoryRepository(deployment_secret="test-deployment-secret"),
    )
    with session_scope(session_factory) as session:
        result = service.answer(session, "private corrupt artifact query", idempotency_key="safe")
        trajectory = (
            session.execute(
                text(
                    """
                SELECT te.result_json, te.process_json
                FROM task_evaluations te
                ORDER BY te.created_at DESC
                LIMIT 1
                """
                )
            )
            .mappings()
            .one()
        )

    assert isinstance(result, AnswerEnvelope)
    assert result.stop_reason is StopReason.RELEASE_UNAVAILABLE
    assert rag.calls == []
    serialized = json.dumps(dict(trajectory), ensure_ascii=False)
    assert "private corrupt artifact query" not in serialized
    assert json.loads(trajectory["process_json"])["release_degraded_reasons"] == [
        "release_corrupt_or_unavailable"
    ]
