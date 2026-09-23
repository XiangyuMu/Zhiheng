from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text


class MaintenanceTriggerKind(StrEnum):
    SAME_FAILURE_THRESHOLD = "same_failure_threshold"
    SAFETY_EXCEPTION = "safety_exception"
    KNOWLEDGE_CONFLICT = "knowledge_conflict"
    RETRIEVAL_FAILURE = "retrieval_failure"


class MaintenanceCadence(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


RetirementAction = Literal["deprecated", "archive", "merge", "revalidate"]


@dataclass(frozen=True, slots=True)
class MaintenanceTrigger:
    kind: MaintenanceTriggerKind
    target_component: str
    evidence_refs: tuple[str, ...]
    task_family: str | None = None
    failure_tag: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaintenanceOutput:
    output_id: str
    output_type: str
    target_component: str
    status: str
    reason: str


def _output_refs_to_json(outputs: Sequence[MaintenanceOutput]) -> list[dict[str, str]]:
    return [
        {
            "output_id": output.output_id,
            "output_type": output.output_type,
        }
        for output in outputs
    ]


def _output_refs_from_json(value: Any) -> tuple[dict[str, str], ...]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, list):
        raise TypeError("maintenance receipt output refs must be a JSON list")
    refs: list[dict[str, str]] = []
    for item in loaded:
        if not isinstance(item, Mapping):
            raise TypeError("maintenance receipt output ref must be a JSON object")
        refs.append(
            {
                "output_id": str(item["output_id"]),
                "output_type": str(item["output_type"]),
            }
        )
    return tuple(refs)


def _json_mapping(value: Any) -> Mapping[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, Mapping):
        raise TypeError("maintenance payload must be a JSON object")
    return loaded


class SleepLearningMaintenanceService:
    """Draft-only maintenance.

    This service never reviews, approves, publishes, or mutates heads.
    """

    def __init__(self, *, same_failure_threshold: int = 3, proposer_id: str = "sleep-learning"):
        if same_failure_threshold <= 0:
            raise ValueError("same_failure_threshold must be positive")
        self._same_failure_threshold = same_failure_threshold
        self._proposer_id = proposer_id

    def handle_event_trigger(
        self,
        session: Session,
        trigger: MaintenanceTrigger,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[MaintenanceOutput, ...]:
        request = {
            "mode": "event",
            "trigger_kind": trigger.kind.value,
            "target_component": trigger.target_component,
            "evidence_refs": list(trigger.evidence_refs),
            "task_family": trigger.task_family,
            "failure_tag": trigger.failure_tag,
            "details": dict(trigger.details),
        }
        return self._run_idempotent(
            session,
            idempotency_key=idempotency_key,
            request=request,
            produce=lambda: self._handle_event_trigger(session, trigger),
        )

    def _handle_event_trigger(
        self,
        session: Session,
        trigger: MaintenanceTrigger,
    ) -> tuple[MaintenanceOutput, ...]:
        if trigger.kind is MaintenanceTriggerKind.SAME_FAILURE_THRESHOLD:
            if not trigger.failure_tag:
                raise ValueError("same failure trigger requires failure_tag")
            if (
                self._count_failure_tag(
                    session,
                    task_family=trigger.task_family,
                    failure_tag=trigger.failure_tag,
                )
                < self._same_failure_threshold
            ):
                return ()
            reason = f"same failure threshold reached: {trigger.failure_tag}"
            return (
                self._create_strategy_proposal_draft(session, trigger, reason=reason),
                self._create_dynamic_eval_case_candidate(session, trigger, reason=reason),
            )

        reason = trigger.kind.value
        if trigger.kind is MaintenanceTriggerKind.SAFETY_EXCEPTION:
            return (
                self._create_dynamic_eval_case_candidate(session, trigger, reason=reason),
                self._create_retention_decision(
                    session,
                    target_component=trigger.target_component,
                    action="revalidate",
                    reason=reason,
                    evidence_refs=trigger.evidence_refs,
                ),
            )
        if trigger.kind in {
            MaintenanceTriggerKind.KNOWLEDGE_CONFLICT,
            MaintenanceTriggerKind.RETRIEVAL_FAILURE,
        }:
            return (self._create_strategy_proposal_draft(session, trigger, reason=reason),)
        raise ValueError(f"unsupported maintenance trigger: {trigger.kind}")

    def run_periodic(
        self,
        session: Session,
        *,
        cadence: MaintenanceCadence,
        target_component: str,
        evidence_refs: Sequence[str] = (),
        idempotency_key: str | None = None,
    ) -> tuple[MaintenanceOutput, ...]:
        request = {
            "mode": "periodic",
            "cadence": cadence.value,
            "target_component": target_component,
            "evidence_refs": list(evidence_refs),
        }
        return self._run_idempotent(
            session,
            idempotency_key=idempotency_key,
            request=request,
            produce=lambda: self._run_periodic(
                session,
                cadence=cadence,
                target_component=target_component,
                evidence_refs=evidence_refs,
            ),
        )

    def _run_periodic(
        self,
        session: Session,
        *,
        cadence: MaintenanceCadence,
        target_component: str,
        evidence_refs: Sequence[str] = (),
    ) -> tuple[MaintenanceOutput, ...]:
        if cadence is MaintenanceCadence.WEEKLY:
            actions: tuple[RetirementAction, ...] = ("revalidate",)
        elif cadence is MaintenanceCadence.MONTHLY:
            actions = ("merge", "deprecated")
        elif cadence is MaintenanceCadence.QUARTERLY:
            actions = ("archive", "revalidate")
        else:
            raise ValueError(f"unsupported maintenance cadence: {cadence}")

        return tuple(
            self._create_retention_decision(
                session,
                target_component=target_component,
                action=action,
                reason=f"{cadence.value} maintenance",
                evidence_refs=tuple(evidence_refs),
            )
            for action in actions
        )

    def _run_idempotent(
        self,
        session: Session,
        *,
        idempotency_key: str | None,
        request: Mapping[str, Any],
        produce: Callable[[], tuple[MaintenanceOutput, ...]],
    ) -> tuple[MaintenanceOutput, ...]:
        if idempotency_key is None:
            return produce()
        key_digest = sha256_text(idempotency_key)
        payload_digest = sha256_json({"request": dict(request)})
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO maintenance_job_locks (
                  id, idempotency_digest, payload_digest
                )
                VALUES (
                  :id, :idempotency_digest, :payload_digest
                )
                """
            ),
            {
                "id": new_id(),
                "idempotency_digest": key_digest,
                "payload_digest": payload_digest,
            },
        )
        lock_inserted = int(session.execute(text("SELECT changes()")).scalar_one()) == 1
        lock = (
            session.execute(
                text(
                    """
                SELECT payload_digest
                FROM maintenance_job_locks
                WHERE idempotency_digest = :idempotency_digest
                """
                ),
                {"idempotency_digest": key_digest},
            )
            .mappings()
            .one()
        )
        if str(lock["payload_digest"]) != payload_digest:
            raise ValueError("maintenance idempotency key reused with different payload")
        existing = (
            session.execute(
                text(
                    """
                SELECT output_refs_json
                FROM maintenance_job_receipts
                WHERE idempotency_digest = :idempotency_digest
                """
                ),
                {"idempotency_digest": key_digest},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return self._load_receipt_outputs(
                session,
                _output_refs_from_json(existing["output_refs_json"]),
            )
        if not lock_inserted:
            raise RuntimeError("maintenance idempotency receipt missing for existing lock")

        outputs = produce()
        session.execute(
            text(
                """
                INSERT INTO maintenance_job_receipts (
                  id, idempotency_digest, output_refs_json
                )
                VALUES (
                  :id, :idempotency_digest, :output_refs_json
                )
                """
            ),
            {
                "id": new_id(),
                "idempotency_digest": key_digest,
                "output_refs_json": json_text(_output_refs_to_json(outputs)),
            },
        )
        return outputs

    def _load_receipt_outputs(
        self,
        session: Session,
        refs: Sequence[Mapping[str, str]],
    ) -> tuple[MaintenanceOutput, ...]:
        return tuple(
            self._load_receipt_output(
                session,
                output_id=ref["output_id"],
                output_type=ref["output_type"],
            )
            for ref in refs
        )

    def _load_receipt_output(
        self,
        session: Session,
        *,
        output_id: str,
        output_type: str,
    ) -> MaintenanceOutput:
        if output_type == "proposal_candidate":
            row = (
                session.execute(
                    text(
                        """
                    SELECT target_component, state, minimal_diff_json
                    FROM evolution_proposals
                    WHERE id = :output_id
                    """
                    ),
                    {"output_id": output_id},
                )
                .mappings()
                .one()
            )
            payload = _json_mapping(row["minimal_diff_json"])
            return MaintenanceOutput(
                output_id=output_id,
                output_type=output_type,
                target_component=str(row["target_component"]),
                status=str(row["state"]),
                reason=str(payload["reason"]),
            )

        if output_type in {
            "strategy_proposal_draft",
            "dynamic_eval_case_candidate",
            "retention_decision",
        }:
            row = (
                session.execute(
                    text(
                        """
                    SELECT status, artifact_json
                    FROM evolution_artifacts
                    WHERE id = :output_id AND artifact_kind = :output_type
                    """
                    ),
                    {"output_id": output_id, "output_type": output_type},
                )
                .mappings()
                .one()
            )
            payload = _json_mapping(row["artifact_json"])
            return MaintenanceOutput(
                output_id=output_id,
                output_type=output_type,
                target_component=str(payload["target_component"]),
                status=str(row["status"]),
                reason=str(payload["reason"]),
            )

        raise ValueError(f"unsupported maintenance receipt output type: {output_type}")

    def record_retirement_decision(
        self,
        session: Session,
        *,
        target_id: str,
        target_component: str,
        action: RetirementAction,
        reason: str,
        evidence_refs: Sequence[str] = (),
        outcome: str = "candidate_decision",
    ) -> MaintenanceOutput:
        if action not in {"deprecated", "archive", "merge", "revalidate"}:
            raise ValueError(f"unsupported retirement action: {action}")
        if not reason:
            raise ValueError("retirement reason is required")

        if action == "deprecated":
            row = (
                session.execute(
                    text("SELECT state FROM evolution_proposals WHERE id = :target_id"),
                    {"target_id": target_id},
                )
                .mappings()
                .first()
            )
            if row is not None and str(row["state"]) != "deprecated":
                session.execute(
                    text(
                        """
                        UPDATE evolution_proposals
                        SET state = 'deprecated', updated_at = CURRENT_TIMESTAMP
                        WHERE id = :target_id
                        """
                    ),
                    {"target_id": target_id},
                )
                self._insert_proposal_event(
                    session,
                    proposal_id=target_id,
                    previous_state=str(row["state"]),
                    next_state="deprecated",
                    reason=reason,
                    payload={
                        "action": action,
                        "reason": reason,
                        "outcome": outcome,
                        "evidence_refs": list(evidence_refs),
                    },
                )

        return self._create_retention_decision(
            session,
            target_component=target_component,
            action=action,
            reason=reason,
            evidence_refs=tuple(evidence_refs),
            target_id=target_id,
            outcome=outcome,
        )

    def _create_strategy_proposal_draft(
        self,
        session: Session,
        trigger: MaintenanceTrigger,
        *,
        reason: str,
    ) -> MaintenanceOutput:
        payload = {
            "output_type": "strategy_proposal_draft",
            "target_component": trigger.target_component,
            "trigger": trigger.kind.value,
            "reason": reason,
            "details": dict(trigger.details),
            "failure_tag": trigger.failure_tag,
            "task_family": trigger.task_family,
            "evidence_refs": list(trigger.evidence_refs),
        }
        artifact_id = self._insert_candidate_artifact(
            session,
            artifact_kind="strategy_proposal_draft",
            target_component=trigger.target_component,
            payload=payload,
            source_ref=trigger.evidence_refs[0] if trigger.evidence_refs else None,
        )
        return MaintenanceOutput(
            output_id=artifact_id,
            output_type="strategy_proposal_draft",
            target_component=trigger.target_component,
            status="draft",
            reason=reason,
        )

    def _create_dynamic_eval_case_candidate(
        self,
        session: Session,
        trigger: MaintenanceTrigger,
        *,
        reason: str,
    ) -> MaintenanceOutput:
        payload = {
            "output_type": "dynamic_eval_case_candidate",
            "target_component": trigger.target_component,
            "trigger": trigger.kind.value,
            "reason": reason,
            "candidate_only": True,
            "may_promote_release": False,
            "evidence_refs": list(trigger.evidence_refs),
            "details": dict(trigger.details),
        }
        artifact_id = self._insert_candidate_artifact(
            session,
            artifact_kind="dynamic_eval_case_candidate",
            target_component=trigger.target_component,
            payload=payload,
            source_ref=trigger.evidence_refs[0] if trigger.evidence_refs else None,
        )
        return MaintenanceOutput(
            output_id=artifact_id,
            output_type="dynamic_eval_case_candidate",
            target_component=trigger.target_component,
            status="draft",
            reason=reason,
        )

    def _create_retention_decision(
        self,
        session: Session,
        *,
        target_component: str,
        action: RetirementAction,
        reason: str,
        evidence_refs: Sequence[str],
        target_id: str | None = None,
        outcome: str = "candidate_decision",
    ) -> MaintenanceOutput:
        payload = {
            "output_type": "retention_decision",
            "target_component": target_component,
            "target_id": target_id,
            "action": action,
            "reason": reason,
            "outcome": outcome,
            "evidence_refs": list(evidence_refs),
            "may_review": False,
            "may_user_approve": False,
            "may_publish": False,
            "may_mutate_head": False,
        }
        artifact_id = self._insert_candidate_artifact(
            session,
            artifact_kind="retention_decision",
            target_component=target_component,
            payload=payload,
            source_ref=evidence_refs[0] if evidence_refs else None,
        )
        return MaintenanceOutput(
            output_id=artifact_id,
            output_type="retention_decision",
            target_component=target_component,
            status="draft",
            reason=reason,
        )

    def _insert_candidate_artifact(
        self,
        session: Session,
        *,
        artifact_kind: str,
        target_component: str,
        payload: Mapping[str, Any],
        source_ref: str | None,
    ) -> str:
        artifact_id = new_id()
        artifact_json = json_text(dict(payload))
        session.execute(
            text(
                """
                INSERT INTO evolution_artifacts (
                  id, artifact_kind, binding_digest, artifact_digest, artifact_json,
                  status, source_ref
                )
                VALUES (
                  :id, :artifact_kind, :binding_digest, :artifact_digest, :artifact_json,
                  'draft', :source_ref
                )
                """
            ),
            {
                "id": artifact_id,
                "artifact_kind": artifact_kind,
                "binding_digest": f"sha256:{sha256_text(target_component)}",
                "artifact_digest": f"sha256:{sha256_json(dict(payload))}",
                "artifact_json": artifact_json,
                "source_ref": source_ref,
            },
        )
        return artifact_id

    def _insert_proposal_event(
        self,
        session: Session,
        *,
        proposal_id: str,
        previous_state: str,
        next_state: str,
        reason: str,
        payload: Mapping[str, Any],
    ) -> None:
        event_payload = {"reason": reason, **dict(payload)}
        session.execute(
            text(
                """
                INSERT INTO proposal_state_events (
                  id, proposal_id, previous_state, next_state, actor_role,
                  actor_id, binding_digest, event_json
                )
                VALUES (
                  :id, :proposal_id, :previous_state, :next_state, 'proposer',
                  :actor_id, :binding_digest, :event_json
                )
                """
            ),
            {
                "id": new_id(),
                "proposal_id": proposal_id,
                "previous_state": previous_state,
                "next_state": next_state,
                "actor_id": self._proposer_id,
                "binding_digest": f"sha256:{sha256_text(proposal_id)}",
                "event_json": json_text(event_payload),
            },
        )

    def _count_failure_tag(
        self,
        session: Session,
        *,
        task_family: str | None,
        failure_tag: str,
    ) -> int:
        params: dict[str, Any] = {"failure_tag": f"%{json.dumps(failure_tag)[1:-1]}%"}
        family_filter = ""
        if task_family:
            params["task_family"] = task_family
            family_filter = "AND tt.task_family = :task_family"
        return int(
            session.execute(
                text(
                    f"""
                    SELECT count(*)
                    FROM task_evaluations te
                    JOIN task_trajectories tt ON tt.id = te.trajectory_id
                    WHERE te.learning_eligible = 0
                      AND te.failure_tags_json LIKE :failure_tag
                      {family_filter}
                    """
                ),
                params,
            ).scalar_one()
        )
