"""Runtime learning signals, failure attribution and draft gap proposals.

The loop is deliberately diagnostic-first: observations are immutable, raw
queries and model output are never copied, and only repeated, authenticated
signals can produce a draft artifact.  Nothing in this module approves or
publishes a proposal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1


class FailureAttributionKind(StrEnum):
    RETRIEVAL_INSUFFICIENT = "retrieval_insufficient"
    KNOWLEDGE_GAP = "knowledge_gap"
    MEMORY_MISUSE = "memory_misuse"
    ANSWER_STRATEGY = "answer_strategy"
    TOOL_MODEL_FAILURE = "tool_model_failure"
    INPUT_AMBIGUITY = "input_ambiguity"


@dataclass(frozen=True, slots=True)
class FailureAttribution:
    kind: FailureAttributionKind
    confidence: float
    explanation: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LearningSignal:
    signal_id: str
    trajectory_id: str
    evaluation_id: str
    attribution: FailureAttributionKind
    attribution_confidence: float
    learning_eligible: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeGapCluster:
    gap_id: str
    cluster_key: str
    attribution: FailureAttributionKind
    occurrence_count: int
    evidence_refs: tuple[str, ...]
    status: str
    proposal_id: str | None = None


class FailureAttributor:
    """Classify a persisted answer outcome without inspecting raw user text."""

    def attribute(self, envelope: TrajectoryEnvelopeV1) -> FailureAttribution:
        result = envelope.result
        process = envelope.process
        quality = envelope.quality
        tags = {str(tag).lower() for tag in envelope.failure_tags}
        stop_reason = str(result.get("stop_reason", "")).lower()
        haystack = " ".join((*tags, stop_reason))

        if (
            "memory_context_changed" in haystack
            or "memory" in haystack
            and ("misuse" in haystack or quality.get("memory_misused") is True)
        ):
            kind = FailureAttributionKind.MEMORY_MISUSE
            explanation = "记忆上下文校验失败或被标记为误用。"
        elif any(
            token in haystack for token in ("model_failed", "invalid_model_output", "tool_failed")
        ):
            kind = FailureAttributionKind.TOOL_MODEL_FAILURE
            explanation = "模型或工具调用未能产出可验证结果。"
        elif any(token in haystack for token in ("ambiguous", "ambiguity", "input_ambiguous")):
            kind = FailureAttributionKind.INPUT_AMBIGUITY
            explanation = "输入信息不足或存在歧义，无法稳定确定任务。"
        elif any(
            token in haystack
            for token in (
                "no_new_authorized_evidence",
                "repeated_query",
                "retrieval",
                "no_authorized",
            )
        ):
            chunks = process.get("context_chunks", result.get("context_chunks", 0))
            if chunks in (None, 0):
                kind = FailureAttributionKind.RETRIEVAL_INSUFFICIENT
                explanation = "检索没有返回足够的授权证据。"
            else:
                kind = FailureAttributionKind.KNOWLEDGE_GAP
                explanation = "已有检索结果但覆盖范围不足以完成回答。"
        elif any(token in haystack for token in ("knowledge", "coverage", "missing")):
            kind = FailureAttributionKind.KNOWLEDGE_GAP
            explanation = "回答所需主题未被当前知识覆盖。"
        else:
            kind = FailureAttributionKind.ANSWER_STRATEGY
            explanation = "证据链存在，但当前回答策略未能完成任务。"

        refs: list[str] = []
        for key in ("retrieval_run_ids", "evidence_refs", "memory_source_ids"):
            value = process.get(key)
            if isinstance(value, (list, tuple)):
                refs.extend(str(item) for item in value if item)
        if not refs:
            refs.append(f"trajectory:{envelope.trajectory_id}")
        confidence = max(0.0, min(1.0, float(envelope.confidence)))
        if kind is FailureAttributionKind.ANSWER_STRATEGY and stop_reason in {"", "failed"}:
            confidence = min(confidence, 0.5)
        return FailureAttribution(
            kind=kind,
            confidence=confidence,
            explanation=explanation,
            evidence_refs=tuple(dict.fromkeys(refs)),
        )


class LearningLoopService:
    """Persist runtime signals and derive idempotent draft knowledge gaps."""

    def __init__(
        self,
        *,
        attributor: FailureAttributor | None = None,
        gap_threshold: int = 3,
    ) -> None:
        if gap_threshold < 1:
            raise ValueError("gap_threshold must be positive")
        self._attributor = attributor or FailureAttributor()
        self._gap_threshold = gap_threshold

    def observe(
        self,
        session: Session,
        envelope: TrajectoryEnvelopeV1,
        *,
        evaluation_id: str | None = None,
    ) -> LearningSignal:
        """Record one immutable observation; duplicate trajectory observations replay."""
        attribution = self._attributor.attribute(envelope)
        if evaluation_id is None:
            row = session.execute(
                text(
                    "SELECT id FROM task_evaluations "
                    "WHERE trajectory_id=:trajectory_id ORDER BY created_at, id LIMIT 1"
                ),
                {"trajectory_id": envelope.trajectory_id},
            ).first()
            if row is None:
                raise ValueError("trajectory evaluation is required before learning observation")
            evaluation_id = str(row[0])
        signal_key = sha256_text(f"{envelope.trajectory_id}:{evaluation_id}:learning.v1")
        evidence_refs = tuple(attribution.evidence_refs)
        eligible = (
            envelope.effective_learning_eligible
            and attribution.confidence >= 0.5
            and envelope.quality.get("evidence_only") is not True
            and not envelope.process.get("release_degraded_reasons")
        )
        payload = {
            "attribution": attribution.kind.value,
            "explanation": attribution.explanation,
            "evidence_refs": list(evidence_refs),
            "signal_key": signal_key,
        }
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO trajectory_learning_signals (
                  id, trajectory_id, evaluation_id, signal_key, signal_kind,
                  attribution_confidence, learning_eligible, evidence_refs_json,
                  details_json
                )
                VALUES (
                  :id, :trajectory_id, :evaluation_id, :signal_key, 'failure',
                  :confidence, :learning_eligible, :evidence_refs_json, :details_json
                )
                """
            ),
            {
                "id": new_id(),
                "trajectory_id": envelope.trajectory_id,
                "evaluation_id": evaluation_id,
                "signal_key": signal_key,
                "confidence": attribution.confidence,
                "learning_eligible": int(eligible),
                "evidence_refs_json": json_text(list(evidence_refs)),
                "details_json": json_text(payload),
            },
        )
        row = (
            session.execute(
                text(
                    """
                SELECT id, learning_eligible, evidence_refs_json
                FROM trajectory_learning_signals WHERE signal_key=:signal_key
                """
                ),
                {"signal_key": signal_key},
            )
            .mappings()
            .one()
        )
        return LearningSignal(
            signal_id=str(row["id"]),
            trajectory_id=envelope.trajectory_id,
            evaluation_id=evaluation_id,
            attribution=attribution.kind,
            attribution_confidence=attribution.confidence,
            learning_eligible=bool(row["learning_eligible"]),
            evidence_refs=tuple(_string_list(row["evidence_refs_json"])),
        )

    def cluster_gaps(
        self,
        session: Session,
        *,
        task_family: str | None = None,
        min_occurrences: int | None = None,
    ) -> tuple[KnowledgeGapCluster, ...]:
        threshold = min_occurrences or self._gap_threshold
        params: dict[str, Any] = {"threshold": threshold}
        family_filter = ""
        if task_family:
            family_filter = "AND tt.task_family=:task_family"
            params["task_family"] = task_family
        rows = (
            session.execute(
                text(
                    f"""
                SELECT tls.attribution, count(*) AS occurrence_count
                FROM trajectory_learning_signals tls
                JOIN task_trajectories tt ON tt.id=tls.trajectory_id
                WHERE tls.signal_kind='failure' AND tls.learning_eligible=0
                  {family_filter}
                GROUP BY tls.attribution
                HAVING count(*) >= :threshold
                ORDER BY tls.attribution
                """
                ),
                params,
            )
            .mappings()
            .all()
        )
        clusters: list[KnowledgeGapCluster] = []
        for row in rows:
            attribution = FailureAttributionKind(str(row["attribution"]))
            cluster_key = sha256_text(f"learning.v1:{task_family or '*'}:{attribution.value}")
            refs = (
                session.execute(
                    text(
                        """
                    SELECT evidence_refs_json FROM trajectory_learning_signals
                    WHERE attribution=:attribution AND signal_kind='failure'
                    ORDER BY created_at DESC, id DESC LIMIT 32
                    """
                    ),
                    {"attribution": attribution.value},
                )
                .scalars()
                .all()
            )
            evidence_refs = tuple(
                dict.fromkeys(ref for value in refs for ref in _string_list(value))
            )
            gap_id = new_id()
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO knowledge_gap_clusters (
                      id, cluster_key, task_family, attribution, occurrence_count,
                      evidence_refs_json, status
                    )
                    VALUES (
                      :id, :cluster_key, :task_family, :attribution, :occurrence_count,
                      :evidence_refs_json, 'open'
                    )
                    """
                ),
                {
                    "id": gap_id,
                    "cluster_key": cluster_key,
                    "task_family": task_family,
                    "attribution": attribution.value,
                    "occurrence_count": int(row["occurrence_count"]),
                    "evidence_refs_json": json_text(list(evidence_refs)),
                },
            )
            existing = (
                session.execute(
                    text(
                        """
                    SELECT id, occurrence_count, evidence_refs_json, status, proposal_id
                    FROM knowledge_gap_clusters WHERE cluster_key=:cluster_key
                    """
                    ),
                    {"cluster_key": cluster_key},
                )
                .mappings()
                .one()
            )
            clusters.append(
                KnowledgeGapCluster(
                    gap_id=str(existing["id"]),
                    cluster_key=cluster_key,
                    attribution=attribution,
                    occurrence_count=int(existing["occurrence_count"]),
                    evidence_refs=tuple(_string_list(existing["evidence_refs_json"])),
                    status=str(existing["status"]),
                    proposal_id=str(existing["proposal_id"]) if existing["proposal_id"] else None,
                )
            )
        return tuple(clusters)

    def generate_draft_proposals(
        self,
        session: Session,
        *,
        task_family: str | None = None,
        min_occurrences: int | None = None,
    ) -> tuple[str, ...]:
        drafts: list[str] = []
        for gap in self.cluster_gaps(
            session, task_family=task_family, min_occurrences=min_occurrences
        ):
            if gap.proposal_id is not None or not gap.evidence_refs:
                continue
            payload = {
                "output_type": "strategy_proposal_draft",
                "target_component": task_family or "retrieval.answer_strategy",
                "gap_id": gap.gap_id,
                "cluster_key": gap.cluster_key,
                "attribution": gap.attribution.value,
                "occurrence_count": gap.occurrence_count,
                "evidence_refs": list(gap.evidence_refs),
                "goal": f"reduce repeated {gap.attribution.value} failures",
                "minimal_change": "evaluate the smallest retrieval or answer-strategy adjustment",
                "expected_benefit": "fewer repeated authenticated failures",
                "risk": "regression in fixed evaluation sets",
                "rollback_target": "current stable release",
                "status": "draft",
            }
            artifact_id = new_id()
            session.execute(
                text(
                    """
                    INSERT INTO evolution_artifacts (
                      id, artifact_kind, binding_digest, artifact_digest, artifact_json,
                      status, source_ref
                    )
                    VALUES (
                      :id, 'strategy_proposal_draft', :binding_digest, :artifact_digest,
                      :artifact_json, 'draft', :source_ref
                    )
                    """
                ),
                {
                    "id": artifact_id,
                    "binding_digest": f"sha256:{sha256_text(gap.cluster_key)}",
                    "artifact_digest": f"sha256:{sha256_json(payload)}",
                    "artifact_json": json_text(payload),
                    "source_ref": gap.gap_id,
                },
            )
            session.execute(
                text(
                    "UPDATE knowledge_gap_clusters SET proposal_id=:proposal_id "
                    "WHERE cluster_key=:cluster_key AND proposal_id IS NULL"
                ),
                {"proposal_id": artifact_id, "cluster_key": gap.cluster_key},
            )
            linked = session.execute(
                text(
                    "SELECT proposal_id FROM knowledge_gap_clusters WHERE cluster_key=:cluster_key"
                ),
                {"cluster_key": gap.cluster_key},
            ).scalar_one()
            if linked == artifact_id:
                drafts.append(artifact_id)
        return tuple(drafts)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(item) for item in value] if isinstance(value, list) else []
