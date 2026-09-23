from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.evolution.trajectories import (
    TrajectoryEnvelopeV1,
    TrajectoryEvidenceState,
    TrajectoryRecordV1,
)


@dataclass(slots=True)
class TrajectoryRepository:
    deployment_secret: str
    session_factory: sessionmaker[Session] | None = None
    _records_by_idempotency_key_digest: dict[str, TrajectoryRecordV1] = field(
        default_factory=dict, init=False, repr=False
    )
    _records_by_trajectory_id: dict[str, TrajectoryRecordV1] = field(
        default_factory=dict, init=False, repr=False
    )

    def ingest(
        self,
        envelope: TrajectoryEnvelopeV1,
        *,
        idempotency_key: str,
        session: Session | None = None,
    ) -> TrajectoryRecordV1:
        if session is None and self.session_factory is not None:
            with session_scope(self.session_factory) as scoped_session:
                return self._ingest_db(
                    scoped_session,
                    envelope,
                    idempotency_key=idempotency_key,
                )
        if session is not None:
            return self._ingest_db(session, envelope, idempotency_key=idempotency_key)
        return self._ingest_memory(envelope, idempotency_key=idempotency_key)

    def ingest_mapping(
        self,
        data: dict[str, Any],
        *,
        idempotency_key: str,
        session: Session | None = None,
    ) -> TrajectoryRecordV1:
        envelope = TrajectoryEnvelopeV1.from_mapping(data)
        return self.ingest(envelope, idempotency_key=idempotency_key, session=session)

    def replay(
        self,
        idempotency_key: str,
        *,
        session: Session | None = None,
    ) -> TrajectoryRecordV1:
        idempotency_key_digest = _idempotency_key_digest(idempotency_key)
        if session is None and self.session_factory is not None:
            with session_scope(self.session_factory) as scoped_session:
                record = self._load_by_idempotency_key_digest(
                    scoped_session, idempotency_key_digest
                )
                if record is None:
                    raise KeyError(f"unknown trajectory idempotency key: {idempotency_key}")
                return record
        if session is not None:
            record = self._load_by_idempotency_key_digest(session, idempotency_key_digest)
            if record is None:
                raise KeyError(f"unknown trajectory idempotency key: {idempotency_key}")
            return record
        try:
            return self._records_by_idempotency_key_digest[idempotency_key_digest]
        except KeyError as exc:
            raise KeyError(f"unknown trajectory idempotency key: {idempotency_key}") from exc

    def get(
        self,
        trajectory_id: str,
        *,
        session: Session | None = None,
    ) -> TrajectoryRecordV1:
        if session is None and self.session_factory is not None:
            with session_scope(self.session_factory) as scoped_session:
                record = self._load_by_trajectory_id(scoped_session, trajectory_id)
                if record is None:
                    raise KeyError(f"unknown trajectory id: {trajectory_id}")
                return record
        if session is not None:
            record = self._load_by_trajectory_id(session, trajectory_id)
            if record is None:
                raise KeyError(f"unknown trajectory id: {trajectory_id}")
            return record
        try:
            return self._records_by_trajectory_id[trajectory_id]
        except KeyError as exc:
            raise KeyError(f"unknown trajectory id: {trajectory_id}") from exc

    def _ingest_db(
        self,
        session: Session,
        envelope: TrajectoryEnvelopeV1,
        *,
        idempotency_key: str,
    ) -> TrajectoryRecordV1:
        idempotency_key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = envelope.canonical_digest()
        existing = self._load_by_idempotency_key_digest(session, idempotency_key_digest)
        if existing is not None:
            if (
                existing.request_digest != request_digest
                or existing.trajectory_id != envelope.trajectory_id
            ):
                raise ValueError("idempotency key already used for a different trajectory")
            return existing

        existing_by_id = self._load_by_trajectory_id(session, envelope.trajectory_id)
        if existing_by_id is not None:
            if existing_by_id.request_digest != request_digest:
                raise ValueError("trajectory id already used for a different trajectory")
            return existing_by_id

        record = TrajectoryRecordV1(
            trajectory_id=envelope.trajectory_id,
            idempotency_key_sha256=idempotency_key_digest,
            request_digest=request_digest,
            deployment_hmac_digest=envelope.deployment_hmac_digest(self.deployment_secret),
            envelope=envelope,
        )
        self._insert_rows(session, record)
        self._cache_record(record)
        return record

    def _ingest_memory(
        self,
        envelope: TrajectoryEnvelopeV1,
        *,
        idempotency_key: str,
    ) -> TrajectoryRecordV1:
        idempotency_key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = envelope.canonical_digest()
        existing = self._records_by_idempotency_key_digest.get(idempotency_key_digest)
        if existing is not None:
            if (
                existing.request_digest != request_digest
                or existing.trajectory_id != envelope.trajectory_id
            ):
                raise ValueError("idempotency key already used for a different trajectory")
            return existing

        existing_by_id = self._records_by_trajectory_id.get(envelope.trajectory_id)
        if existing_by_id is not None:
            if existing_by_id.request_digest != request_digest:
                raise ValueError("trajectory id already used for a different trajectory")
            return existing_by_id

        record = TrajectoryRecordV1(
            trajectory_id=envelope.trajectory_id,
            idempotency_key_sha256=idempotency_key_digest,
            request_digest=request_digest,
            deployment_hmac_digest=envelope.deployment_hmac_digest(self.deployment_secret),
            envelope=envelope,
        )
        self._cache_record(record)
        return record

    def _insert_rows(self, session: Session, record: TrajectoryRecordV1) -> None:
        envelope = record.envelope
        evidence_refs = {
            "deployment_hmac_digest": record.deployment_hmac_digest,
            "events": [event.canonical_payload() for event in envelope.events],
            "event_chain_digest": envelope.event_chain_digest,
            "event_count": len(envelope.events),
            "event_hashes": [event.event_hash for event in envelope.events],
            "idempotency_key_sha256": record.idempotency_key_sha256,
            "learning_eligible": envelope.effective_learning_eligible,
            "user_feedback": envelope.user_feedback,
            "task_id": envelope.task_id,
            "created_at": envelope.created_at,
            "request_digest": record.request_digest,
        }
        canary_observation = envelope.process.get("canary_observation")
        if isinstance(canary_observation, dict):
            evidence_refs["canary_observation"] = canary_observation
        session.execute(
            text(
                """
                INSERT INTO task_trajectories (
                  id, task_family, agent_version, knowledge_version, environment_version,
                  status, evidence_refs_json
                )
                VALUES (
                  :id, :task_family, :agent_version, :knowledge_version, :environment_version,
                  :status, :evidence_refs_json
                )
                """
            ),
            {
                "id": record.trajectory_id,
                "task_family": envelope.task_family,
                "agent_version": envelope.agent_version,
                "knowledge_version": envelope.knowledge_version,
                "environment_version": envelope.environment_version,
                "status": envelope.evidence_state.value,
                "evidence_refs_json": json_text(evidence_refs),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO task_evaluations (
                  id, trajectory_id, result_json, process_json, quality_json,
                  failure_tags_json, confidence, learning_eligible
                )
                VALUES (
                  :id, :trajectory_id, :result_json, :process_json, :quality_json,
                  :failure_tags_json, :confidence, :learning_eligible
                )
                """
            ),
            {
                "id": new_id(),
                "trajectory_id": record.trajectory_id,
                "result_json": json_text(envelope.result),
                "process_json": json_text(envelope.process),
                "quality_json": json_text(envelope.quality),
                "failure_tags_json": json_text(list(envelope.failure_tags)),
                "confidence": envelope.confidence,
                "learning_eligible": envelope.effective_learning_eligible,
            },
        )

    def _load_by_idempotency_key_digest(
        self,
        session: Session,
        idempotency_key_digest: str,
    ) -> TrajectoryRecordV1 | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, task_family, agent_version, knowledge_version, environment_version,
                       status, evidence_refs_json
                FROM task_trajectories
                WHERE json_extract(evidence_refs_json, '$.idempotency_key_sha256') = :digest
                LIMIT 1
                """
                ),
                {"digest": idempotency_key_digest},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return self._record_from_row(session, row)

    def _load_by_trajectory_id(
        self, session: Session, trajectory_id: str
    ) -> TrajectoryRecordV1 | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, task_family, agent_version, knowledge_version, environment_version,
                       status, evidence_refs_json
                FROM task_trajectories
                WHERE id = :trajectory_id
                LIMIT 1
                """
                ),
                {"trajectory_id": trajectory_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return self._record_from_row(session, row)

    def _record_from_row(self, session: Session, row: Any) -> TrajectoryRecordV1:
        evidence_refs = _json_object(row["evidence_refs_json"])
        evaluation_row = (
            session.execute(
                text(
                    """
                SELECT result_json, process_json, quality_json, failure_tags_json,
                       confidence, learning_eligible
                FROM task_evaluations
                WHERE trajectory_id = :trajectory_id
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
                ),
                {"trajectory_id": row["id"]},
            )
            .mappings()
            .first()
        )
        if evaluation_row is None:
            raise ValueError("trajectory record is missing evaluation row")
        evidence_state = TrajectoryEvidenceState(str(row["status"]))
        envelope = TrajectoryEnvelopeV1.from_mapping(
            {
                "trajectory_id": row["id"],
                "task_id": str(evidence_refs.get("task_id", row["id"])),
                "task_family": row["task_family"],
                "agent_version": row["agent_version"],
                "knowledge_version": row["knowledge_version"],
                "environment_version": row["environment_version"],
                "created_at": str(evidence_refs.get("created_at", "1970-01-01T00:00:00Z")),
                "result": _json_object(evaluation_row["result_json"]),
                "process": _json_object(evaluation_row["process_json"]),
                "quality": _json_object(evaluation_row["quality_json"]),
                "failure_tags": _string_list(evaluation_row["failure_tags_json"]),
                "confidence": float(evaluation_row["confidence"]),
                "user_feedback": evidence_refs.get("user_feedback"),
                "learning_eligible": bool(evaluation_row["learning_eligible"]),
                "evidence_state": evidence_state.value,
                "events": _json_list(evidence_refs.get("events", ())),
            }
        )
        record = TrajectoryRecordV1(
            trajectory_id=row["id"],
            idempotency_key_sha256=str(evidence_refs["idempotency_key_sha256"]),
            request_digest=str(evidence_refs["request_digest"]),
            deployment_hmac_digest=str(evidence_refs["deployment_hmac_digest"]),
            envelope=envelope,
        )
        self._verify_loaded_record(record, evidence_refs)
        self._cache_record(record)
        return record

    def _cache_record(self, record: TrajectoryRecordV1) -> None:
        self._records_by_idempotency_key_digest[record.idempotency_key_sha256] = record
        self._records_by_trajectory_id[record.trajectory_id] = record

    def _verify_loaded_record(
        self,
        record: TrajectoryRecordV1,
        evidence_refs: dict[str, Any],
    ) -> None:
        if record.request_digest != record.envelope.canonical_digest():
            raise ValueError("trajectory record failed request digest verification")
        expected_hmac = record.envelope.deployment_hmac_digest(self.deployment_secret)
        if not hmac.compare_digest(record.deployment_hmac_digest, expected_hmac):
            raise ValueError("trajectory record failed deployment HMAC verification")
        expected_chain = record.envelope.event_chain_digest
        if str(evidence_refs.get("event_chain_digest", expected_chain)) != expected_chain:
            raise ValueError("trajectory record failed event-chain verification")
        stored_hashes = evidence_refs.get("event_hashes")
        if stored_hashes is not None and _string_list(stored_hashes) != tuple(
            event.event_hash for event in record.envelope.events
        ):
            raise ValueError("trajectory record failed event hash verification")


def _idempotency_key_digest(idempotency_key: str) -> str:
    return f"sha256:{sha256_text(idempotency_key)}"


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise TypeError("expected JSON object")


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return decoded
    if isinstance(value, tuple):
        return list(value)
    return []


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return tuple(item for item in decoded if isinstance(item, str))
    return ()
