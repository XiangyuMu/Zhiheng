from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_text


class GapReasonCode(StrEnum):
    MISSING_MATERIAL = "missing_material"
    LOW_QUALITY = "low_quality"
    STALE = "stale"
    VIEWPOINT_DIVERSITY = "viewpoint_diversity"
    INSUFFICIENT_DEPTH = "insufficient_depth"


@dataclass(frozen=True)
class FormalGoalRef:
    formal_memory_id: str
    formal_version_id: str
    state_key: str
    effective_generation: int


@dataclass(frozen=True)
class KnowledgeGapRecommendation:
    id: str
    goal: FormalGoalRef
    domain_id: str
    reason_code: GapReasonCode
    why: str
    benefit: str
    evidence: tuple[str, ...]
    missing_coverage: tuple[str, ...]
    suggested_search_terms: tuple[str, ...]
    source_types: tuple[str, ...]
    priority: int
    learning_cost: str
    requirement_key: str


class KnowledgeGapService:
    _forbidden_deficit_terms = ("你不会", "用户不会", "你缺乏能力", "能力不足", "用户能力不足")

    def recommend(
        self,
        session: Session,
        *,
        goal: FormalGoalRef,
        domain_id: str,
        reason_code: GapReasonCode,
        missing_coverage: Sequence[str],
        evidence: Sequence[str] = (),
        suggested_search_terms: Sequence[str] = (),
        source_types: Sequence[str] = ("paper", "book", "official_doc"),
        priority: int = 3,
        learning_cost: str = "medium",
    ) -> tuple[KnowledgeGapRecommendation, ...]:
        formal = self._current_formal_goal(session, goal)
        run_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO knowledge_gap_runs (
                  id, trigger_kind, goal_state_key, goal_formal_memory_id,
                  goal_formal_version_id, goal_generation, coverage_manifest_hash,
                  status, usage_json
                )
                VALUES (
                  :id, 'manual', :goal_state_key, :goal_formal_memory_id,
                  :goal_formal_version_id, :goal_generation, :coverage_manifest_hash,
                  :status, :usage_json
                )
                """
            ),
            {
                "id": run_id,
                "goal_state_key": goal.state_key,
                "goal_formal_memory_id": goal.formal_memory_id if formal else None,
                "goal_formal_version_id": goal.formal_version_id if formal else None,
                "goal_generation": goal.effective_generation if formal else None,
                "coverage_manifest_hash": sha256_text("|".join(sorted(missing_coverage))),
                "status": "completed" if formal else "no_current_formal_goal",
                "usage_json": json_text(
                    {"recommendation_count": 1 if formal and missing_coverage else 0}
                ),
            },
        )
        if not formal or not missing_coverage:
            return ()

        requirement_key = sha256_text(
            "|".join(
                [
                    goal.state_key,
                    str(goal.effective_generation),
                    domain_id,
                    reason_code.value,
                    *missing_coverage,
                ]
            )
        )
        duplicate = session.execute(
            text(
                """
                SELECT 1
                FROM knowledge_gap_recommendations
                WHERE goal_state_key = :goal_state_key
                  AND json_extract(missing_coverage_json, '$.requirement_key') = :requirement_key
                LIMIT 1
                """
            ),
            {"goal_state_key": goal.state_key, "requirement_key": requirement_key},
        ).first()
        if duplicate is not None:
            return ()

        why = f"知识库覆盖不足：{', '.join(missing_coverage)}。"
        benefit = "补充这些资料可以让后续回答和决策更有证据基础。"
        self._assert_non_deficit_language(why, benefit)
        recommendation = KnowledgeGapRecommendation(
            id=new_id(),
            goal=goal,
            domain_id=domain_id,
            reason_code=reason_code,
            why=why,
            benefit=benefit,
            evidence=tuple(evidence),
            missing_coverage=tuple(missing_coverage),
            suggested_search_terms=tuple(suggested_search_terms or missing_coverage),
            source_types=tuple(source_types),
            priority=priority,
            learning_cost=learning_cost,
            requirement_key=requirement_key,
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_gap_recommendations (
                  id, run_id, goal_state_key, domain_id, recommendation_status,
                  why, benefit, missing_coverage_json, evidence_refs_json,
                  candidate_query_json, auto_ingest_allowed
                )
                VALUES (
                  :id, :run_id, :goal_state_key, :domain_id, 'serving',
                  :why, :benefit, :missing_coverage_json, :evidence_refs_json,
                  :candidate_query_json, 0
                )
                """
            ),
            {
                "id": recommendation.id,
                "run_id": run_id,
                "goal_state_key": goal.state_key,
                "domain_id": domain_id,
                "why": recommendation.why,
                "benefit": recommendation.benefit,
                "missing_coverage_json": json_text(
                    {
                        "reason_code": reason_code.value,
                        "items": list(recommendation.missing_coverage),
                        "requirement_key": requirement_key,
                        "priority": priority,
                        "learning_cost": learning_cost,
                    }
                ),
                "evidence_refs_json": json_text(list(recommendation.evidence)),
                "candidate_query_json": json_text(
                    {
                        "suggested_search_terms": list(recommendation.suggested_search_terms),
                        "source_types": list(recommendation.source_types),
                    }
                ),
            },
        )
        return (recommendation,)

    def list_recommendations(
        self,
        session: Session,
        *,
        goal_id: str | None = None,
    ) -> tuple[KnowledgeGapRecommendation, ...]:
        query = """
            SELECT
              r.id, r.goal_state_key, r.domain_id, r.why, r.benefit,
              r.missing_coverage_json, r.evidence_refs_json, r.candidate_query_json,
              g.goal_formal_memory_id, g.goal_formal_version_id, g.goal_generation
            FROM knowledge_gap_recommendations r
            JOIN knowledge_gap_runs g ON g.id = r.run_id
            JOIN current_formal_memory m
              ON m.id = g.goal_formal_memory_id
             AND m.current_version_id = g.goal_formal_version_id
             AND m.effective_generation = g.goal_generation
             AND m.state_key = g.goal_state_key
             AND m.state_key LIKE 'goal.%'
            WHERE r.recommendation_status = 'serving'
              AND (:goal_id IS NULL OR g.goal_formal_memory_id = :goal_id)
              AND NOT EXISTS (
                SELECT 1
                FROM knowledge_gap_recommendations dismissed
                WHERE dismissed.recommendation_status = 'dismissed'
                  AND json_extract(
                    dismissed.missing_coverage_json,
                    '$.dismissed_gap_id'
                  ) = r.id
              )
            ORDER BY r.created_at DESC, r.id DESC
            """
        rows = session.execute(
            text(query),
            {"goal_id": goal_id},
        ).mappings().all()
        return tuple(_recommendation_from_row(row) for row in rows)

    def dismiss(self, session: Session, gap_id: str, *, reason: str) -> bool:
        row = session.execute(
            text(
                """
                SELECT
                  r.run_id, r.goal_state_key, r.domain_id, r.why, r.benefit,
                  r.missing_coverage_json
                FROM knowledge_gap_recommendations r
                JOIN knowledge_gap_runs g ON g.id = r.run_id
                JOIN current_formal_memory m
                  ON m.id = g.goal_formal_memory_id
                 AND m.current_version_id = g.goal_formal_version_id
                 AND m.effective_generation = g.goal_generation
                 AND m.state_key = g.goal_state_key
                 AND m.state_key LIKE 'goal.%'
                WHERE r.id = :id
                  AND r.recommendation_status = 'serving'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_gap_recommendations dismissed
                    WHERE dismissed.recommendation_status = 'dismissed'
                      AND json_extract(
                        dismissed.missing_coverage_json,
                        '$.dismissed_gap_id'
                      ) = r.id
                  )
                """
            ),
            {"id": gap_id},
        ).mappings().first()
        if row is None:
            return False

        missing = _json_object(row["missing_coverage_json"])
        missing["dismissed_gap_id"] = gap_id
        missing["dismiss_reason_hash"] = sha256_text(reason)
        session.execute(
            text(
                """
                INSERT INTO knowledge_gap_recommendations (
                  id, run_id, goal_state_key, domain_id, recommendation_status,
                  why, benefit, missing_coverage_json, evidence_refs_json,
                  candidate_query_json, auto_ingest_allowed
                )
                VALUES (
                  :id, :run_id, :goal_state_key, :domain_id, 'dismissed',
                  :why, :benefit, :missing_coverage_json, '[]', '{}', 0
                )
                """
            ),
            {
                "id": new_id(),
                "run_id": row["run_id"],
                "goal_state_key": row["goal_state_key"],
                "domain_id": row["domain_id"],
                "why": row["why"],
                "benefit": row["benefit"],
                "missing_coverage_json": json_text(missing),
            },
        )
        return True

    def _current_formal_goal(self, session: Session, goal: FormalGoalRef) -> bool:
        row = session.execute(
            text(
                """
                SELECT 1
                FROM current_formal_memory
                WHERE id = :formal_memory_id
                  AND current_version_id = :formal_version_id
                  AND state_key = :state_key
                  AND effective_generation = :effective_generation
                  AND state_key LIKE 'goal.%'
                """
            ),
            {
                "formal_memory_id": goal.formal_memory_id,
                "formal_version_id": goal.formal_version_id,
                "state_key": goal.state_key,
                "effective_generation": goal.effective_generation,
            },
        ).first()
        return row is not None

    def _assert_non_deficit_language(self, *values: str) -> None:
        text_value = "\n".join(values)
        if any(term in text_value for term in self._forbidden_deficit_terms):
            raise ValueError("knowledge gap language must describe knowledge-base coverage")


def _recommendation_from_row(row: Any) -> KnowledgeGapRecommendation:
    missing = _json_object(row["missing_coverage_json"])
    candidate_query = _json_object(row["candidate_query_json"])
    reason_code = GapReasonCode(str(missing.get("reason_code", GapReasonCode.MISSING_MATERIAL)))
    return KnowledgeGapRecommendation(
        id=str(row["id"]),
        goal=FormalGoalRef(
            formal_memory_id=str(row["goal_formal_memory_id"]),
            formal_version_id=str(row["goal_formal_version_id"]),
            state_key=str(row["goal_state_key"]),
            effective_generation=int(row["goal_generation"]),
        ),
        domain_id=str(row["domain_id"]),
        reason_code=reason_code,
        why=str(row["why"]),
        benefit=str(row["benefit"]),
        evidence=tuple(_string_list(row["evidence_refs_json"])),
        missing_coverage=tuple(_string_list(missing.get("items"))),
        suggested_search_terms=tuple(_string_list(candidate_query.get("suggested_search_terms"))),
        source_types=tuple(_string_list(candidate_query.get("source_types"))),
        priority=int(missing.get("priority", 3)),
        learning_cost=str(missing.get("learning_cost", "medium")),
        requirement_key=str(missing.get("requirement_key", "")),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        import json

        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        import json

        decoded = json.loads(value)
        value = decoded
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def recommend_knowledge_gaps(
    session: Session,
    *,
    goal: FormalGoalRef,
    domain_id: str,
    reason_code: GapReasonCode,
    missing_coverage: Sequence[str],
) -> tuple[KnowledgeGapRecommendation, ...]:
    return KnowledgeGapService().recommend(
        session,
        goal=goal,
        domain_id=domain_id,
        reason_code=reason_code,
        missing_coverage=missing_coverage,
    )


__all__ = [
    "FormalGoalRef",
    "GapReasonCode",
    "KnowledgeGapRecommendation",
    "KnowledgeGapService",
    "recommend_knowledge_gaps",
]
