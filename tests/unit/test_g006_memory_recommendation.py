from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evaluation.g006_memory_recommendation import (
    PendingGoalCandidateRef,
    execute_memory_recommendation_probe,
)
from zhiheng.gaps import (
    FormalGoalRef,
    GapReasonCode,
    KnowledgeGapRecommendation,
    KnowledgeGapService,
)
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _seed_recommendation_inputs(
    session: Session,
) -> tuple[FormalGoalRef, PendingGoalCandidateRef, PendingGoalCandidateRef]:
    repository = MemoryRepository()
    confirmed = repository.commit_explicit_memory(
        session,
        MemoryValue(
            memory_type="goal",
            state_key="goal.recommendation",
            value={"goal": "build a personal knowledge base agent"},
        ),
        operation_key="g006-recommendation-formal-goal",
    )
    assert confirmed.formal_memory_id is not None
    assert confirmed.formal_version_id is not None
    assert confirmed.generation is not None

    same_key_candidate_id = repository.propose_candidate(
        session,
        MemoryCandidateInput(
            candidate_type="inferred",
            memory_type="goal",
            state_key="goal.recommendation",
            proposed_value={"goal": "candidate must not replace formal"},
            rationale="g006 synthetic same-key candidate",
            source_kind="agent_inferred",
            confidence=0.71,
        ),
    )
    candidate_only_id = repository.propose_candidate(
        session,
        MemoryCandidateInput(
            candidate_type="inferred",
            memory_type="goal",
            state_key="goal.photography",
            proposed_value={"goal": "candidate-only goal must not emit recommendations"},
            rationale="g006 synthetic candidate-only goal",
            source_kind="agent_inferred",
            confidence=0.69,
        ),
    )
    same_key_candidate = _candidate_ref(session, same_key_candidate_id)
    candidate_only = _candidate_ref(session, candidate_only_id)
    formal_goal = FormalGoalRef(
        formal_memory_id=confirmed.formal_memory_id,
        formal_version_id=confirmed.formal_version_id,
        state_key="goal.recommendation",
        effective_generation=confirmed.generation,
    )
    return formal_goal, same_key_candidate, candidate_only


def _candidate_ref(session: Session, candidate_id: str) -> PendingGoalCandidateRef:
    row = (
        session.execute(
            text(
                """
            SELECT id, current_version_id, state_key
            FROM memory_candidates
            WHERE id = :id
            """
            ),
            {"id": candidate_id},
        )
        .mappings()
        .one()
    )
    return PendingGoalCandidateRef(
        candidate_id=str(row["id"]),
        candidate_version_id=str(row["current_version_id"]),
        state_key=str(row["state_key"]),
    )


def test_recommendation_probe_uses_real_service_and_candidates_stay_invisible(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_scope(session_factory) as session:
        formal_goal, same_key_candidate, candidate_only = _seed_recommendation_inputs(session)
        facts, outcomes = execute_memory_recommendation_probe(
            session,
            formal_goal=formal_goal,
            same_key_candidate=same_key_candidate,
            candidate_only_goal=candidate_only,
        )

    assert outcomes == {
        "recommendation.formal_goal_visible": True,
        "recommendation.same_key_candidate_cannot_replace_formal": True,
        "recommendation.candidate_only_goal_not_visible": True,
    }
    assert facts["execution_profile"] == "real_gap_recommendation_formal_goal_only"
    assert facts["visible_recommendation_count"] == 1
    assert facts["visible_for_formal_goal_count"] == 1
    assert facts["same_key_candidate_recommendation_ids"] == ()
    assert facts["candidate_only_recommendation_ids"] == ()
    assert facts["pending_candidate_rows_before"] == facts["pending_candidate_rows_after"]
    assert facts["pending_candidate_rows_after"][same_key_candidate.candidate_id]["status"] == (
        "pending_confirmation"
    )
    assert facts["pending_candidate_rows_after"][candidate_only.candidate_id]["status"] == (
        "pending_confirmation"
    )


def test_probe_fails_when_candidate_only_goal_emits_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_scope(session_factory) as session:
        formal_goal, same_key_candidate, candidate_only = _seed_recommendation_inputs(session)
        original_recommend = KnowledgeGapService.recommend

        def unauthorized_recommend(
            self: KnowledgeGapService,
            session: Session,
            *,
            goal: FormalGoalRef,
            domain_id: str,
            reason_code: GapReasonCode,
            missing_coverage: tuple[str, ...],
            evidence: tuple[str, ...] = (),
            suggested_search_terms: tuple[str, ...] = (),
            source_types: tuple[str, ...] = ("paper", "book", "official_doc"),
            priority: int = 3,
            learning_cost: str = "medium",
        ) -> tuple[KnowledgeGapRecommendation, ...]:
            if goal.formal_memory_id == candidate_only.candidate_id:
                return (_fake_recommendation(goal, domain_id, missing_coverage),)
            return original_recommend(
                self,
                session,
                goal=goal,
                domain_id=domain_id,
                reason_code=reason_code,
                missing_coverage=missing_coverage,
                evidence=evidence,
                suggested_search_terms=suggested_search_terms,
                source_types=source_types,
                priority=priority,
                learning_cost=learning_cost,
            )

        monkeypatch.setattr(KnowledgeGapService, "recommend", unauthorized_recommend)
        facts, outcomes = execute_memory_recommendation_probe(
            session,
            formal_goal=formal_goal,
            same_key_candidate=same_key_candidate,
            candidate_only_goal=candidate_only,
        )

    assert outcomes["recommendation.formal_goal_visible"] is True
    assert outcomes["recommendation.same_key_candidate_cannot_replace_formal"] is True
    assert outcomes["recommendation.candidate_only_goal_not_visible"] is False
    assert facts["candidate_only_recommendation_ids"]


def test_probe_fails_when_visible_list_leaks_candidate_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_scope(session_factory) as session:
        formal_goal, same_key_candidate, candidate_only = _seed_recommendation_inputs(session)
        original_list = KnowledgeGapService.list_recommendations

        def leaking_list(
            self: KnowledgeGapService, session: Session, *, goal_id: str | None = None
        ) -> tuple[KnowledgeGapRecommendation, ...]:
            items = original_list(self, session, goal_id=goal_id)
            if goal_id is not None:
                return items
            candidate_goal = FormalGoalRef(
                formal_memory_id=same_key_candidate.candidate_id,
                formal_version_id=same_key_candidate.candidate_version_id,
                state_key=formal_goal.state_key,
                effective_generation=formal_goal.effective_generation,
            )
            return items + (
                _fake_recommendation(
                    candidate_goal,
                    "technology.ai",
                    ("synthetic same-key candidate coverage",),
                ),
            )

        monkeypatch.setattr(KnowledgeGapService, "list_recommendations", leaking_list)
        facts, outcomes = execute_memory_recommendation_probe(
            session,
            formal_goal=formal_goal,
            same_key_candidate=same_key_candidate,
            candidate_only_goal=candidate_only,
        )

    assert outcomes["recommendation.formal_goal_visible"] is False
    assert outcomes["recommendation.same_key_candidate_cannot_replace_formal"] is True
    assert outcomes["recommendation.candidate_only_goal_not_visible"] is False
    assert same_key_candidate.candidate_id in str(facts["visible_goal_refs"])


def _fake_recommendation(
    goal: FormalGoalRef, domain_id: str, missing_coverage: tuple[str, ...]
) -> KnowledgeGapRecommendation:
    return KnowledgeGapRecommendation(
        id=new_id(),
        goal=goal,
        domain_id=domain_id,
        reason_code=GapReasonCode.MISSING_MATERIAL,
        why="知识库覆盖不足：synthetic mutation。",
        benefit="补充后可以验证候选泄漏会被探针发现。",
        evidence=("mutation",),
        missing_coverage=missing_coverage,
        suggested_search_terms=missing_coverage,
        source_types=("paper",),
        priority=3,
        learning_cost="medium",
        requirement_key=sha256_text("|".join((goal.state_key, *missing_coverage))),
    )
