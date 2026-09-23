"""Learning-source provenance is distinct from fixed-set release authorization."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_json
from zhiheng.evolution.trajectories import (
    TrajectoryEnvelopeV1,
    TrajectoryEvidenceState,
    TrajectoryRecordV1,
)
from zhiheng.evolution.trajectory_repository import TrajectoryRepository

POLICY_VERSION = "learning-evidence.v1"
TARGET_COMPONENT = "retrieval.answer_strategy"
_DIAGNOSTIC_OUTCOMES = frozenset(
    {
        "model_failed",
        "invalid_model_output",
        "citation_validation_failed",
        "budget_exhausted",
        "no_new_authorized_evidence",
    }
)


class LearningEvidenceUse(StrEnum):
    PROPOSAL_ELIGIBLE = "proposal_eligible"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    REFUSED = "refused"


class LearningEvidencePolicy:
    """Classify authenticated observations, never approve an artifact or a release."""

    @staticmethod
    def classify(record: TrajectoryRecordV1) -> LearningEvidenceUse:
        envelope = record.envelope
        event_types = {event.event_type for event in envelope.events}
        if (
            envelope.evidence_state is not TrajectoryEvidenceState.ACTIVE
            or envelope.task_family != TARGET_COMPONENT
            or not {"result", "process", "quality"} <= event_types
            or envelope.quality.get("no_raw_query_or_model_output") is not True
            or envelope.quality.get("evidence_only") is not False
            or not envelope.process.get("release_id")
            or not envelope.process.get("binding_digest")
            or not envelope.process.get("artifact_digest")
            or envelope.process.get("release_state") != "stable"
            or envelope.process.get("release_degraded_reasons") != []
        ):
            return LearningEvidenceUse.REFUSED
        # Reuse the existing uncertainty/content gate without changing the signed record
        # or its learning_eligible flag. A false flag does not become a successful sample.
        content_check = TrajectoryEnvelopeV1.from_mapping(
            {
                **envelope.canonical_payload(),
                "learning_eligible": True,
            }
        )
        if not content_check.effective_learning_eligible:
            return LearningEvidenceUse.REFUSED
        outcome = envelope.result.get("stop_reason")
        if not isinstance(outcome, str):
            return LearningEvidenceUse.REFUSED
        if (
            envelope.effective_learning_eligible
            and envelope.confidence > 0
            and outcome == "completed"
            and not envelope.failure_tags
            and envelope.quality.get("completed") is True
        ):
            return LearningEvidenceUse.PROPOSAL_ELIGIBLE
        if (
            outcome in _DIAGNOSTIC_OUTCOMES
            and envelope.failure_tags
            and envelope.quality.get("completed") is False
        ):
            return LearningEvidenceUse.DIAGNOSTIC_ONLY
        return LearningEvidenceUse.REFUSED


@dataclass(frozen=True, slots=True)
class LearningSourceRef:
    evaluation_id: str
    trajectory_id: str
    request_digest: str
    query_sha256: str
    event_chain_digest: str
    environment_version: str
    knowledge_version: str
    created_at: str
    use: LearningEvidenceUse


@dataclass(frozen=True, slots=True)
class ProposalSourceGraphV1:
    """Digest/ref-only origin; raw observations are reloaded, not copied into receipts."""

    task_family: str
    baseline_release_id: str
    baseline_binding_digest: str
    baseline_artifact_digest: str
    trigger_sources: tuple[LearningSourceRef, ...]
    support_sources: tuple[LearningSourceRef, ...]
    counter_sources: tuple[LearningSourceRef, ...]
    applicability: tuple[str, ...]
    exceptions: tuple[str, ...]

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "proposal-source-graph.v1",
            "policy_version": POLICY_VERSION,
            **asdict(self),
        }

    def canonical_digest(self) -> str:
        return f"sha256:{sha256_json(self.canonical_payload())}"


class LearningEvidenceLoader:
    def __init__(self, deployment_secret: str) -> None:
        self._repository = TrajectoryRepository(deployment_secret=deployment_secret)

    def load(
        self,
        session: Session,
        evaluation_id: str,
    ) -> tuple[LearningSourceRef, TrajectoryRecordV1]:
        row = session.execute(
            text("SELECT trajectory_id FROM task_evaluations WHERE id=:id"),
            {"id": evaluation_id},
        ).first()
        if row is None:
            raise ValueError("learning source evaluation does not exist")
        trajectory_id = str(row[0])
        count = session.execute(
            text("SELECT count(*) FROM task_evaluations WHERE trajectory_id=:id"),
            {"id": trajectory_id},
        ).scalar_one()
        if count != 1:
            raise ValueError("learning source requires one unambiguous signed evaluation")
        record = self._repository.get(trajectory_id, session=session)
        envelope = record.envelope
        use = LearningEvidencePolicy.classify(record)
        query_digest = envelope.process.get("query_sha256")
        if (
            not isinstance(query_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", query_digest) is None
            or envelope.task_id != f"answer:{query_digest}"
        ):
            raise ValueError("learning source requires an authenticated query identity")
        return LearningSourceRef(
            evaluation_id=evaluation_id,
            trajectory_id=trajectory_id,
            query_sha256=query_digest,
            request_digest=record.request_digest,
            event_chain_digest=envelope.event_chain_digest,
            environment_version=envelope.environment_version,
            knowledge_version=envelope.knowledge_version,
            created_at=envelope.created_at,
            use=use,
        ), record

    def build_graph(
        self,
        session: Session,
        *,
        baseline_release_id: str,
        baseline_binding_digest: str,
        baseline_artifact_digest: str,
        trigger_evaluation_ids: tuple[str, ...],
        support_evaluation_ids: tuple[str, ...],
        counter_evaluation_ids: tuple[str, ...],
    ) -> ProposalSourceGraphV1:
        all_ids = (*trigger_evaluation_ids, *support_evaluation_ids, *counter_evaluation_ids)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("learning source groups must be disjoint and duplicate-free")
        if (
            len(trigger_evaluation_ids) < 3
            or not support_evaluation_ids
            or not counter_evaluation_ids
        ):
            raise ValueError("proposal needs three diagnostics plus support and counter sources")
        if len(all_ids) > 32:
            raise ValueError("proposal source graph exceeds its bounded source budget")
        loaded = {identifier: self.load(session, identifier) for identifier in sorted(all_ids)}
        environments = set()
        trajectories = set()
        for ref, record in loaded.values():
            process = record.envelope.process
            if (
                process.get("release_id") != baseline_release_id
                or process.get("binding_digest") != baseline_binding_digest
                or process.get("artifact_digest") != baseline_artifact_digest
            ):
                raise ValueError("learning source does not match the frozen baseline")
            environments.add(ref.environment_version)
            trajectories.add(ref.trajectory_id)
        if len(environments) != 1 or len(trajectories) != len(all_ids):
            raise ValueError("source environments must match and trajectories must be distinct")
        primary_queries = {
            loaded[identifier][0].query_sha256
            for identifier in (*trigger_evaluation_ids, *support_evaluation_ids)
        }
        if any(
            loaded[identifier][0].query_sha256 in primary_queries
            for identifier in counter_evaluation_ids
        ):
            raise ValueError("counter sources require a distinct authenticated query")

        def group(
            ids: tuple[str, ...],
            expected: LearningEvidenceUse,
        ) -> tuple[LearningSourceRef, ...]:
            refs = tuple(loaded[identifier][0] for identifier in sorted(ids))
            if any(ref.use is not expected for ref in refs):
                raise ValueError("learning source is not eligible for its proposed evidence role")
            return refs

        return ProposalSourceGraphV1(
            task_family=TARGET_COMPONENT,
            baseline_release_id=baseline_release_id,
            baseline_binding_digest=baseline_binding_digest,
            baseline_artifact_digest=baseline_artifact_digest,
            trigger_sources=group(trigger_evaluation_ids, LearningEvidenceUse.DIAGNOSTIC_ONLY),
            support_sources=group(support_evaluation_ids, LearningEvidenceUse.PROPOSAL_ELIGIBLE),
            counter_sources=group(counter_evaluation_ids, LearningEvidenceUse.PROPOSAL_ELIGIBLE),
            applicability=(
                "same_task_family",
                "same_baseline",
                "same_environment",
                "distinct_counter_query",
            ),
            exceptions=(
                "diagnostics_do_not_authorize_publication",
                "fixed_validation_still_required",
            ),
        )

    def build_graph_for_failure(
        self,
        session: Session,
        *,
        baseline_release_id: str,
        baseline_binding_digest: str,
        baseline_artifact_digest: str,
        failure_tag: str,
    ) -> ProposalSourceGraphV1:
        """Select a bounded graph from authenticated persisted observations.

        The maintenance signal is only a selector; it cannot provide source IDs.
        Every selected row is reloaded and verified by ``build_graph``.
        """
        if not failure_tag or len(failure_tag) > 128:
            raise ValueError("invalid maintenance failure tag")
        rows = (
            session.execute(
                text(
                    """
            SELECT id, failure_tags_json, process_json, created_at
            FROM task_evaluations
            ORDER BY created_at DESC, id DESC
            LIMIT 256
            """
                )
            )
            .mappings()
            .all()
        )
        diagnostics: list[str] = []
        successes: list[str] = []
        for row in rows:
            tags = row["failure_tags_json"]
            tags = tags if isinstance(tags, list) else json.loads(tags)
            process = row["process_json"]
            process = process if isinstance(process, dict) else json.loads(process)
            if (
                process.get("release_id") != baseline_release_id
                or process.get("binding_digest") != baseline_binding_digest
                or process.get("artifact_digest") != baseline_artifact_digest
            ):
                continue
            identifier = str(row["id"])
            _, record = self.load(session, identifier)
            use = LearningEvidencePolicy.classify(record)
            if use is LearningEvidenceUse.DIAGNOSTIC_ONLY and failure_tag in tags:
                diagnostics.append(identifier)
            elif use is LearningEvidenceUse.PROPOSAL_ELIGIBLE:
                successes.append(identifier)
        if len(diagnostics) < 3 or len(successes) < 2:
            raise ValueError("persisted evidence is insufficient for a strategy proposal")
        # build_graph enforces distinct query hashes and all authenticated invariants.
        refs = [self.load(session, item)[0] for item in successes]
        distinct: list[str] = []
        seen_queries: set[str] = set()
        for identifier, ref in zip(successes, refs, strict=True):
            if ref.query_sha256 not in seen_queries:
                distinct.append(identifier)
                seen_queries.add(ref.query_sha256)
            if len(distinct) == 2:
                break
        if len(distinct) < 2:
            raise ValueError("persisted support and counter evidence must differ")
        return self.build_graph(
            session,
            baseline_release_id=baseline_release_id,
            baseline_binding_digest=baseline_binding_digest,
            baseline_artifact_digest=baseline_artifact_digest,
            trigger_evaluation_ids=tuple(diagnostics[:3]),
            support_evaluation_ids=(distinct[0],),
            counter_evaluation_ids=(distinct[1],),
        )

    def verify_graph(
        self,
        session: Session,
        payload: dict[str, Any],
        expected_digest: str,
    ) -> ProposalSourceGraphV1:
        """Rebuild the origin from authenticated DB records, not serialized eligibility."""
        if f"sha256:{sha256_json(payload)}" != expected_digest:
            raise ValueError("proposal source graph digest mismatch")

        def identifiers(name: str) -> tuple[str, ...]:
            refs = payload.get(name)
            if not isinstance(refs, (list, tuple)) or not refs:
                raise ValueError("proposal source graph requires nonempty evidence groups")
            ids = []
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("evaluation_id"), str):
                    raise ValueError("proposal source graph has invalid source identity")
                ids.append(ref["evaluation_id"])
            return tuple(ids)

        baseline = {}
        for name in ("baseline_release_id", "baseline_binding_digest", "baseline_artifact_digest"):
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError("proposal source graph has invalid baseline identity")
            baseline[name] = value
        graph = self.build_graph(
            session,
            **baseline,
            trigger_evaluation_ids=identifiers("trigger_sources"),
            support_evaluation_ids=identifiers("support_sources"),
            counter_evaluation_ids=identifiers("counter_sources"),
        )
        if graph.canonical_digest() != expected_digest:
            raise ValueError("proposal source graph differs from authenticated current evidence")
        return graph
