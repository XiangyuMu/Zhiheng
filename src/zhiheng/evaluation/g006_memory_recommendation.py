"""Real recommendation isolation probe for the G006 memory fixed cases.

The probe uses a migrated SQLAlchemy session and the production
KnowledgeGapService. It verifies that recommendations are visible only when
they bind to the current formal goal generation, while pending goal candidates
cannot replace or create serving recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from zhiheng.gaps import FormalGoalRef, GapReasonCode, KnowledgeGapService


@dataclass(frozen=True, slots=True)
class PendingGoalCandidateRef:
    candidate_id: str
    candidate_version_id: str
    state_key: str


def execute_memory_recommendation_probe(
    session: Session,
    *,
    formal_goal: FormalGoalRef,
    same_key_candidate: PendingGoalCandidateRef,
    candidate_only_goal: PendingGoalCandidateRef,
) -> tuple[dict[str, Any], dict[str, bool]]:
    service = KnowledgeGapService()
    formal_coverage = ("synthetic formal coverage: evaluation sources",)
    same_key_candidate_coverage = ("synthetic same-key candidate coverage",)
    candidate_only_coverage = ("synthetic candidate-only coverage",)

    pending_candidates_before = _pending_candidate_rows(
        session, candidates=(same_key_candidate, candidate_only_goal)
    )

    formal_recommendations = service.recommend(
        session,
        goal=formal_goal,
        domain_id="technology.ai",
        reason_code=GapReasonCode.MISSING_MATERIAL,
        missing_coverage=formal_coverage,
        evidence=("g006.synthetic.coverage.audit",),
        suggested_search_terms=("synthetic evaluation sources",),
    )
    same_key_candidate_recommendations = service.recommend(
        session,
        goal=FormalGoalRef(
            formal_memory_id=same_key_candidate.candidate_id,
            formal_version_id=same_key_candidate.candidate_version_id,
            state_key=formal_goal.state_key,
            effective_generation=formal_goal.effective_generation,
        ),
        domain_id="technology.ai",
        reason_code=GapReasonCode.MISSING_MATERIAL,
        missing_coverage=same_key_candidate_coverage,
    )
    candidate_only_recommendations = service.recommend(
        session,
        goal=FormalGoalRef(
            formal_memory_id=candidate_only_goal.candidate_id,
            formal_version_id=candidate_only_goal.candidate_version_id,
            state_key=candidate_only_goal.state_key,
            effective_generation=1,
        ),
        domain_id="lifestyle.learning",
        reason_code=GapReasonCode.MISSING_MATERIAL,
        missing_coverage=candidate_only_coverage,
    )

    visible = service.list_recommendations(session)
    visible_for_formal = service.list_recommendations(session, goal_id=formal_goal.formal_memory_id)
    pending_candidates_after = _pending_candidate_rows(
        session, candidates=(same_key_candidate, candidate_only_goal)
    )
    raw_recommendation_rows = (
        session.execute(
            text(
                """
            SELECT
              r.id, r.goal_state_key, r.recommendation_status,
              g.goal_formal_memory_id, g.goal_formal_version_id, g.goal_generation
            FROM knowledge_gap_recommendations r
            JOIN knowledge_gap_runs g ON g.id = r.run_id
            ORDER BY r.created_at, r.id
            """
            )
        )
        .mappings()
        .all()
    )

    visible_ids = tuple(item.id for item in visible)
    formal_ids = tuple(item.id for item in formal_recommendations)
    same_key_candidate_ids = tuple(item.id for item in same_key_candidate_recommendations)
    candidate_only_ids = tuple(item.id for item in candidate_only_recommendations)
    forbidden_coverage_visible = any(
        coverage in item.missing_coverage
        for item in visible
        for coverage in (*same_key_candidate_coverage, *candidate_only_coverage)
    )
    visible_binds_formal_goal = all(
        item.goal == formal_goal and item.goal.state_key == formal_goal.state_key
        for item in visible
    )

    facts: dict[str, Any] = {
        "execution_profile": "real_gap_recommendation_formal_goal_only",
        "formal_goal": {
            "formal_memory_id": formal_goal.formal_memory_id,
            "formal_version_id": formal_goal.formal_version_id,
            "state_key": formal_goal.state_key,
            "effective_generation": formal_goal.effective_generation,
        },
        "same_key_candidate": _candidate_fact(same_key_candidate),
        "candidate_only_goal": _candidate_fact(candidate_only_goal),
        "pending_candidate_rows_before": pending_candidates_before,
        "pending_candidate_rows_after": pending_candidates_after,
        "formal_recommendation_ids": formal_ids,
        "same_key_candidate_recommendation_ids": same_key_candidate_ids,
        "candidate_only_recommendation_ids": candidate_only_ids,
        "visible_recommendation_ids": visible_ids,
        "visible_goal_refs": tuple(
            {
                "formal_memory_id": item.goal.formal_memory_id,
                "formal_version_id": item.goal.formal_version_id,
                "state_key": item.goal.state_key,
                "effective_generation": item.goal.effective_generation,
            }
            for item in visible
        ),
        "visible_for_formal_goal_ids": tuple(item.id for item in visible_for_formal),
        "visible_recommendation_count": len(visible),
        "visible_for_formal_goal_count": len(visible_for_formal),
        "raw_recommendation_rows": [dict(row) for row in raw_recommendation_rows],
        "formal_missing_coverage": formal_coverage,
        "candidate_missing_coverage": same_key_candidate_coverage + candidate_only_coverage,
    }
    outcomes = {
        "recommendation.formal_goal_visible": (
            len(formal_recommendations) == 1
            and visible_ids == formal_ids
            and tuple(item.id for item in visible_for_formal) == formal_ids
            and visible_binds_formal_goal
        ),
        "recommendation.same_key_candidate_cannot_replace_formal": (
            not same_key_candidate_recommendations
            and pending_candidates_before[same_key_candidate.candidate_id]["status"]
            == "pending_confirmation"
            and pending_candidates_after[same_key_candidate.candidate_id]["status"]
            == "pending_confirmation"
        ),
        "recommendation.candidate_only_goal_not_visible": (
            not candidate_only_recommendations
            and candidate_only_goal.candidate_id
            not in {str(row["goal_formal_memory_id"]) for row in raw_recommendation_rows}
            and not forbidden_coverage_visible
        ),
    }
    return facts, outcomes


def _pending_candidate_rows(
    session: Session, *, candidates: tuple[PendingGoalCandidateRef, ...]
) -> dict[str, dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
            SELECT id, state_key, status, current_version_id
            FROM memory_candidates
            WHERE id IN :ids
            """
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": tuple(candidate.candidate_id for candidate in candidates)},
        )
        .mappings()
        .all()
    )
    return {str(row["id"]): dict(row) for row in rows}


def _candidate_fact(candidate: PendingGoalCandidateRef) -> dict[str, str]:
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_version_id": candidate.candidate_version_id,
        "state_key": candidate.state_key,
    }


__all__ = ["PendingGoalCandidateRef", "execute_memory_recommendation_probe"]
