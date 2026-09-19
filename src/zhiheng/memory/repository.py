from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json


@dataclass(frozen=True)
class MemoryValue:
    memory_type: str
    state_key: str
    value: dict[str, Any]
    sensitivity_level: str = "private"
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_sensitivity: str = "persistent"
    confidence_explanation: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.valid_from, self.valid_to)


@dataclass(frozen=True)
class MemoryCandidateInput:
    candidate_type: str
    memory_type: str
    state_key: str
    proposed_value: dict[str, Any]
    rationale: str
    source_kind: str
    confidence: float
    sensitivity_level: str = "private"
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_sensitivity: str = "persistent"
    confidence_explanation: dict[str, Any] | None = None
    extracted_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.valid_from, self.valid_to)


@dataclass(frozen=True)
class ConfirmationResult:
    receipt_id: str
    decision_id: str
    formal_memory_id: str | None
    formal_version_id: str | None
    generation: int | None
    status: str


class StaleConfirmationError(ValueError):
    pass


class OperationConflictError(ValueError):
    pass


class StaleMemoryStateError(ValueError):
    pass


L0_MAX_ITEMS = 32
L0_MAX_SERIALIZED_BYTES = 8192
L0_ALLOWED_PREFIXES = ("identity.", "role.", "goal.", "project.", "constraint.")


class MemoryRepository:
    def commit_explicit_memory(
        self,
        session: Session,
        memory: MemoryValue,
        *,
        operation_key: str,
        evidence_refs: list[dict[str, Any]] | None = None,
    ) -> ConfirmationResult:
        request_hash = _request_hash(
            operation_type="explicit_direct_commit",
            target_type="state_key",
            target_id=memory.state_key,
            expected_version_id=None,
            expected_generation=None,
            payload={"memory": _memory_payload(memory), "evidence_refs": evidence_refs or []},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)

        current = self._current_formal_memory_for_state(session, memory.state_key)
        formal_id = str(current["id"]) if current is not None else new_id()
        version_id = new_id()
        generation = self._next_generation(session, memory.state_key)
        receipt_id = self._insert_receipt(
            session, operation_key, "explicit_direct_commit", request_hash
        )
        value_json = json_text(memory.value)
        now_generation = generation
        if current is None:
            session.execute(
                text(
                    """
                    INSERT INTO formal_memories (
                      id, memory_type, state_key, status, current_version_id,
                      current_generation, sensitivity_level, confidence, valid_from, valid_to,
                      time_sensitivity, confidence_explanation, origin_kind
                    )
                    VALUES (
                      :id, :memory_type, :state_key, 'formal_current', :current_version_id,
                      :current_generation, :sensitivity_level, :confidence, :valid_from, :valid_to,
                      :time_sensitivity, :confidence_explanation, 'explicit_direct'
                    )
                    """
                ),
                {
                    "id": formal_id,
                    "memory_type": memory.memory_type,
                    "state_key": memory.state_key,
                    "current_version_id": version_id,
                    "current_generation": now_generation,
                    "sensitivity_level": memory.sensitivity_level,
                    "confidence": memory.confidence,
                    "valid_from": memory.valid_from or _utc_now(),
                    "valid_to": memory.valid_to,
                    "time_sensitivity": memory.time_sensitivity,
                    "confidence_explanation": json_text(
                        memory.confidence_explanation or _confidence_explanation(memory.confidence)
                    ),
                },
            )
        else:
            session.execute(
                text(
                    """
                    UPDATE formal_memories
                    SET memory_type = :memory_type,
                        current_version_id = :current_version_id,
                        current_generation = :current_generation,
                        sensitivity_level = :sensitivity_level,
                        confidence = :confidence,
                        valid_from = :valid_from,
                        valid_to = :valid_to,
                        time_sensitivity = :time_sensitivity,
                        confidence_explanation = :confidence_explanation,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {
                    "id": formal_id,
                    "memory_type": memory.memory_type,
                    "current_version_id": version_id,
                    "current_generation": now_generation,
                    "sensitivity_level": memory.sensitivity_level,
                    "confidence": memory.confidence,
                    "valid_from": memory.valid_from or _utc_now(),
                    "valid_to": memory.valid_to,
                    "time_sensitivity": memory.time_sensitivity,
                    "confidence_explanation": json_text(
                        memory.confidence_explanation or _confidence_explanation(memory.confidence)
                    ),
                },
            )
        session.execute(
            text(
                """
                INSERT INTO formal_memory_versions (
                  id, formal_memory_id, version_no, value_json, change_reason,
                  created_by_role, generation, status, source_kind
                )
                VALUES (
                  :id, :formal_memory_id, :version_no, :value_json, 'explicit direct commit',
                  'user', :generation, 'current', 'explicit_direct'
                )
                """
            ),
            {
                "id": version_id,
                "formal_memory_id": formal_id,
                "version_no": self._next_formal_version(session, formal_id),
                "value_json": value_json,
                "generation": now_generation,
            },
        )
        self._replace_current_state(
            session, memory.state_key, formal_id, version_id, now_generation
        )
        self._insert_generation_event(
            session,
            state_key=memory.state_key,
            generation=now_generation,
            event_type="formal_committed" if current is None else "formal_version_appended",
            receipt_id=receipt_id,
            reason="explicit direct commit",
        )
        if current is not None:
            self._copy_evidence_refs(
                session,
                target_type="formal_memory",
                target_id=formal_id,
                source_version_id=str(current["current_version_id"]),
                target_version_id=version_id,
            )
        self._insert_evidence_refs(
            session,
            target_type="formal_memory",
            target_id=formal_id,
            target_version_id=version_id,
            refs=evidence_refs or [],
        )
        result = ConfirmationResult(
            receipt_id=receipt_id,
            decision_id="",
            formal_memory_id=formal_id,
            formal_version_id=version_id,
            generation=now_generation,
            status="completed",
        )
        self._complete_receipt(session, receipt_id, result)
        return result

    def append_formal_version(
        self,
        session: Session,
        *,
        formal_memory_id: str,
        value: dict[str, Any],
        operation_key: str,
        change_reason: str,
        expected_etag: str | None = None,
    ) -> ConfirmationResult:
        request_hash = _request_hash(
            operation_type="append_formal_memory_version",
            target_type="formal_memory",
            target_id=formal_memory_id,
            expected_version_id=None,
            expected_generation=None,
            payload={"value": value, "change_reason": change_reason},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)

        memory = self._formal_memory(session, formal_memory_id)
        if memory is None:
            raise ValueError("formal memory not found")
        self._require_formal_etag(memory, expected_etag)
        if memory["status"] != "formal_current" or not self._is_serving_formal_memory(
            session, formal_memory_id
        ):
            raise ValueError("formal memory is not current")

        receipt_id = self._insert_receipt(
            session, operation_key, "append_formal_memory_version", request_hash
        )
        generation = self._next_generation(session, str(memory["state_key"]))
        version_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO formal_memory_versions (
                  id, formal_memory_id, version_no, value_json, change_reason,
                  created_by_role, generation, status, source_kind
                )
                VALUES (
                  :id, :formal_memory_id, :version_no, :value_json, :change_reason,
                  'user', :generation, 'current', 'user_edit'
                )
                """
            ),
            {
                "id": version_id,
                "formal_memory_id": formal_memory_id,
                "version_no": self._next_formal_version(session, formal_memory_id),
                "value_json": json_text(value),
                "change_reason": change_reason[:512],
                "generation": generation,
            },
        )
        session.execute(
            text(
                """
                UPDATE formal_memories
                SET current_version_id = :version_id,
                    current_generation = :generation,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :formal_memory_id
                """
            ),
            {
                "version_id": version_id,
                "generation": generation,
                "formal_memory_id": formal_memory_id,
            },
        )
        self._replace_current_state(
            session,
            str(memory["state_key"]),
            formal_memory_id,
            version_id,
            generation,
        )
        self._copy_evidence_refs(
            session,
            target_type="formal_memory",
            target_id=formal_memory_id,
            source_version_id=str(memory["current_version_id"]),
            target_version_id=version_id,
        )
        self._insert_generation_event(
            session,
            state_key=str(memory["state_key"]),
            generation=generation,
            event_type="formal_version_appended",
            receipt_id=receipt_id,
            reason=change_reason,
        )
        result = ConfirmationResult(
            receipt_id, "", formal_memory_id, version_id, generation, "completed"
        )
        self._complete_receipt(session, receipt_id, result)
        return result

    def propose_candidate(self, session: Session, item: MemoryCandidateInput) -> str:
        if item.candidate_type not in {"explicit_extracted", "inferred"}:
            raise ValueError("candidate_type must be explicit_extracted or inferred")
        candidate_id = new_id()
        version_id = new_id()
        value_json = json_text(item.proposed_value)
        session.execute(
            text(
                """
                INSERT INTO memory_candidates (
                  id, candidate_type, memory_type, state_key, status, current_version_id,
                  source_kind, rationale, confidence, confidence_explanation,
                  valid_from, valid_to, time_sensitivity, extracted_at, sensitivity_level
                )
                VALUES (
                  :id, :candidate_type, :memory_type, :state_key, 'pending_confirmation',
                  :current_version_id, :source_kind, :rationale, :confidence,
                  :confidence_explanation, :valid_from, :valid_to, :time_sensitivity,
                  :extracted_at, :sensitivity_level
                )
                """
            ),
            {
                "id": candidate_id,
                "candidate_type": item.candidate_type,
                "memory_type": item.memory_type,
                "state_key": item.state_key,
                "current_version_id": version_id,
                "source_kind": item.source_kind,
                "rationale": item.rationale,
                "confidence": item.confidence,
                "confidence_explanation": json_text(
                    item.confidence_explanation or _confidence_explanation(item.confidence)
                ),
                "valid_from": item.valid_from,
                "valid_to": item.valid_to,
                "time_sensitivity": item.time_sensitivity,
                "extracted_at": item.extracted_at or _utc_now(),
                "sensitivity_level": item.sensitivity_level,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO memory_candidate_versions (
                  id, candidate_id, version_no, value_json, change_reason,
                  created_by_role, status
                )
                VALUES (
                  :id, :candidate_id, 1, :value_json, 'candidate created',
                  'agent', 'active'
                )
                """
            ),
            {"id": version_id, "candidate_id": candidate_id, "value_json": value_json},
        )
        self._insert_evidence_refs(
            session,
            target_type="memory_candidate",
            target_id=candidate_id,
            target_version_id=version_id,
            refs=item.evidence_refs,
        )
        self._record_conflicts_for_candidate(session, candidate_id, version_id)
        self._record_candidate_evidence(session, candidate_id, version_id, item.evidence_refs)
        return candidate_id

    def list_conflicts(
        self, session: Session, *, status: str = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT *
                FROM memory_conflicts
                WHERE (:status = 'all' OR status = :status)
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"status": status, "limit": max(1, min(limit, 500))},
        ).mappings()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("candidate_value_json", "formal_value_json"):
                if item.get(key) is not None:
                    item[key.removesuffix("_json")] = _json_dict(item.pop(key))
            result.append(item)
        return result

    def list_expiry(
        self, session: Session, *, state: str = "all", within_days: int = 7, limit: int = 100
    ) -> list[dict[str, Any]]:
        cutoff = _utc_now() + timedelta(days=max(0, within_days))
        predicates = ["fm.valid_to IS NOT NULL"]
        if state == "expired":
            predicates.append("datetime(fm.valid_to) <= datetime('now')")
        elif state == "expiring":
            predicates.extend(
                [
                    "datetime(fm.valid_to) > datetime('now')",
                    "datetime(fm.valid_to) <= datetime(:cutoff)",
                ]
            )
        rows = session.execute(
            text(
                f"""
                SELECT fm.id, fm.memory_type, fm.state_key, fm.status, fm.valid_from, fm.valid_to,
                       fm.time_sensitivity, fm.confidence, fmv.id AS version_id,
                       fmv.value_json
                FROM formal_memories fm
                JOIN formal_memory_versions fmv ON fmv.id = fm.current_version_id
                WHERE {' AND '.join(predicates)}
                ORDER BY datetime(fm.valid_to), fm.updated_at DESC
                LIMIT :limit
                """
            ),
            {"cutoff": cutoff, "limit": max(1, min(limit, 500))},
        ).mappings()
        result = []
        now = _utc_now()
        for row in rows:
            item = dict(row)
            item["value"] = _json_dict(item.pop("value_json"))
            valid_to = _coerce_datetime(item["valid_to"])
            item["expiry_state"] = "expired" if valid_to <= now else "expiring"
            item["days_remaining"] = max(0, (valid_to - now).days)
            result.append(item)
        return result

    def timeline(
        self,
        session: Session,
        *,
        formal_memory_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        predicates = ["fm.status <> 'privacy_erased'"]
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if formal_memory_id:
            predicates.append("fm.id = :formal_memory_id")
            params["formal_memory_id"] = formal_memory_id
        if since:
            predicates.append("datetime(fmv.created_at) >= datetime(:since)")
            params["since"] = since
        if until:
            predicates.append("datetime(fmv.created_at) <= datetime(:until)")
            params["until"] = until
        rows = session.execute(
            text(
                f"""
                SELECT fm.id AS formal_memory_id, fm.state_key, fm.memory_type,
                       fm.status AS memory_status, fm.valid_from, fm.valid_to,
                       fmv.id AS version_id, fmv.version_no, fmv.value_json,
                       fmv.change_reason, fmv.created_by_role, fmv.source_kind,
                       fmv.generation, fmv.created_at
                FROM formal_memory_versions fmv
                JOIN formal_memories fm ON fm.id = fmv.formal_memory_id
                WHERE {' AND '.join(predicates)}
                ORDER BY datetime(fmv.created_at) DESC, fmv.version_no DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings()
        result = []
        for row in rows:
            item = dict(row)
            item["value"] = _json_dict(item.pop("value_json"))
            item["event_type"] = "version"
            result.append(item)
        return result

    def _record_conflicts_for_candidate(
        self, session: Session, candidate_id: str, version_id: str
    ) -> None:
        candidate = self._candidate(session, candidate_id)
        if candidate is None:
            return
        formal = self._current_formal_memory_for_state(session, str(candidate["state_key"]))
        if formal is None:
            return
        candidate_value = self._candidate_version(session, version_id)
        if _json_dict(candidate_value["value_json"]) == _json_dict(formal["value_json"]):
            return
        session.execute(
            text(
                """
                INSERT INTO memory_conflicts (
                  id, state_key, memory_type, candidate_id, candidate_version_id,
                  formal_memory_id, formal_version_id, candidate_value_json,
                  formal_value_json, candidate_confidence, formal_confidence
                )
                VALUES (
                  :id, :state_key, :memory_type, :candidate_id, :candidate_version_id,
                  :formal_memory_id, :formal_version_id, :candidate_value_json,
                  :formal_value_json, :candidate_confidence, :formal_confidence
                )
                """
            ),
            {
                "id": new_id(),
                "state_key": candidate["state_key"],
                "memory_type": candidate["memory_type"],
                "candidate_id": candidate_id,
                "candidate_version_id": version_id,
                "formal_memory_id": formal["id"],
                "formal_version_id": formal["current_version_id"],
                "candidate_value_json": json_text(_json_dict(candidate_value["value_json"])),
                "formal_value_json": json_text(_json_dict(formal["value_json"])),
                "candidate_confidence": candidate["confidence"],
                "formal_confidence": formal["confidence"],
            },
        )

    def _record_candidate_evidence(
        self, session: Session, candidate_id: str, version_id: str, refs: list[dict[str, Any]]
    ) -> None:
        for ref in refs:
            if not any(
                ref.get(name) is not None
                for name in (
                    "conversation_id",
                    "message_id",
                    "excerpt",
                    "message_start",
                    "message_end",
                )
            ):
                continue
            session.execute(
                text(
                    """
                    INSERT INTO memory_candidate_evidence (
                      id, candidate_id, candidate_version_id, conversation_id, message_id,
                      message_start, message_end, excerpt, support_type, extracted_at
                    )
                    VALUES (
                      :id, :candidate_id, :candidate_version_id, :conversation_id, :message_id,
                      :message_start, :message_end, :excerpt, :support_type, :extracted_at
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "candidate_id": candidate_id,
                    "candidate_version_id": version_id,
                    "conversation_id": ref.get("conversation_id"),
                    "message_id": ref.get("message_id"),
                    "message_start": ref.get("message_start"),
                    "message_end": ref.get("message_end"),
                    "excerpt": ref.get("excerpt"),
                    "support_type": ref.get("support_type", "supporting"),
                    "extracted_at": ref.get("extracted_at") or _utc_now(),
                },
            )

    def request_confirmation(
        self,
        session: Session,
        *,
        candidate_id: str,
        risk_level: str = "medium",
        expires_at: datetime | None = None,
    ) -> str:
        candidate = self._candidate(session, candidate_id)
        if candidate is None or candidate["status"] not in {"pending_confirmation", "edited"}:
            raise ValueError("candidate is not confirmable")
        request_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO memory_confirmation_requests (
                  id, candidate_id, candidate_version_id, status, risk_level,
                  proposed_value_hash, expires_at
                )
                VALUES (
                  :id, :candidate_id, :candidate_version_id, 'pending', :risk_level,
                  :proposed_value_hash, :expires_at
                )
                """
            ),
            {
                "id": request_id,
                "candidate_id": candidate_id,
                "candidate_version_id": candidate["current_version_id"],
                "risk_level": risk_level,
                "proposed_value_hash": self._candidate_value_hash(
                    session, str(candidate["current_version_id"])
                ),
                "expires_at": expires_at or (_utc_now() + timedelta(days=1)),
            },
        )
        return request_id

    def edit_candidate(
        self,
        session: Session,
        *,
        candidate_id: str,
        new_value: dict[str, Any],
        change_reason: str = "user edit",
        editor_role: str = "user",
    ) -> str:
        candidate = self._candidate(session, candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        if candidate["status"] not in {"pending_confirmation", "edited"}:
            raise ValueError("candidate is not editable")
        source_version_id = str(candidate["current_version_id"])
        next_version = self._next_candidate_version(session, candidate_id)
        version_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO memory_candidate_versions (
                  id, candidate_id, version_no, value_json, change_reason,
                  created_by_role, status
                )
                VALUES (
                  :id, :candidate_id, :version_no, :value_json,
                  :change_reason, :created_by_role, 'active'
                )
                """
            ),
            {
                "id": version_id,
                "candidate_id": candidate_id,
                "version_no": next_version,
                "value_json": json_text(new_value),
                "change_reason": change_reason[:512],
                "created_by_role": editor_role,
            },
        )
        session.execute(
            text(
                """
                UPDATE memory_candidates
                SET status = 'edited',
                    current_version_id = :version_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :candidate_id
                """
            ),
            {"candidate_id": candidate_id, "version_id": version_id},
        )
        self._copy_evidence_refs(
            session,
            target_type="memory_candidate",
            target_id=candidate_id,
            source_version_id=source_version_id,
            target_version_id=version_id,
        )
        session.execute(
            text(
                """
                UPDATE memory_confirmation_requests
                SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = :candidate_id
                  AND status = 'pending'
                """
            ),
            {"candidate_id": candidate_id},
        )
        return version_id

    def confirm_request(
        self,
        session: Session,
        *,
        request_id: str,
        operation_key: str,
        edited_value: dict[str, Any] | None = None,
    ) -> ConfirmationResult:
        request = self._request(session, request_id)
        if request is None:
            raise StaleConfirmationError("confirmation request is stale or closed")
        candidate = self._candidate(session, str(request["candidate_id"]))
        if candidate is None:
            raise StaleConfirmationError("candidate missing")
        request_hash = _request_hash(
            operation_type="confirm_memory",
            target_type="memory_candidate",
            target_id=str(request["candidate_id"]),
            expected_version_id=str(request["candidate_version_id"]),
            expected_generation=None,
            payload={"request_id": request_id, "edited_value": edited_value},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        if request["status"] != "pending":
            raise StaleConfirmationError("confirmation request is stale or closed")
        receipt_id = self._insert_receipt(session, operation_key, "confirm_memory", request_hash)
        try:
            result = self._confirm_request(session, request, candidate, receipt_id, edited_value)
        except Exception:
            self._fail_receipt(session, receipt_id)
            raise
        self._complete_receipt(session, receipt_id, result)
        return result

    def reject_request(
        self,
        session: Session,
        *,
        request_id: str,
        operation_key: str,
    ) -> ConfirmationResult:
        request = self._request(session, request_id)
        if request is None:
            raise StaleConfirmationError("confirmation request is stale or closed")
        request_hash = _request_hash(
            operation_type="reject_memory",
            target_type="memory_candidate",
            target_id=str(request["candidate_id"]),
            expected_version_id=str(request["candidate_version_id"]),
            expected_generation=None,
            payload={"request_id": request_id},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        if request["status"] != "pending":
            raise StaleConfirmationError("confirmation request is stale or closed")
        receipt_id = self._insert_receipt(session, operation_key, "reject_memory", request_hash)
        if not self._request_hash_still_matches(session, request):
            self._fail_receipt(session, receipt_id)
            raise StaleConfirmationError("confirmation request is stale or closed")
        decision_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO memory_confirmation_decisions (
                  id, request_id, decision, final_value_json
                )
                VALUES (:id, :request_id, 'rejected', NULL)
                """
            ),
            {"id": decision_id, "request_id": request_id},
        )
        session.execute(
            text("UPDATE memory_confirmation_requests SET status = 'rejected' WHERE id = :id"),
            {"id": request_id},
        )
        session.execute(
            text("UPDATE memory_candidates SET status = 'rejected' WHERE id = :candidate_id"),
            {"candidate_id": request["candidate_id"]},
        )
        result = ConfirmationResult(receipt_id, decision_id, None, None, None, "rejected")
        self._complete_receipt(session, receipt_id, result)
        return result

    def batch_confirm(
        self,
        session: Session,
        *,
        request_ids: list[str],
        operation_key: str,
    ) -> list[ConfirmationResult]:
        savepoint = session.begin_nested()
        try:
            results = [
                self.confirm_request(
                    session,
                    request_id=request_id,
                    operation_key=f"{operation_key}:{request_id}",
                )
                for request_id in request_ids
            ]
        except Exception:
            savepoint.rollback()
            raise
        else:
            savepoint.commit()
            return results

    def l0_context(self, session: Session) -> dict[str, dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT fm.state_key, fm.memory_type, fmv.value_json
                FROM memory_current_state mcs
                JOIN formal_memories fm ON fm.id = mcs.formal_memory_id
                JOIN formal_memory_versions fmv ON fmv.id = mcs.formal_version_id
                WHERE fm.status = 'formal_current'
                  AND fm.current_version_id = mcs.formal_version_id
                  AND fm.current_generation = mcs.effective_generation
                  AND datetime(fm.valid_from) <= datetime('now')
                  AND (fm.valid_to IS NULL OR datetime(fm.valid_to) > datetime('now'))
                  AND (
                    fm.state_key LIKE 'identity.%'
                    OR fm.state_key LIKE 'role.%'
                    OR fm.state_key LIKE 'goal.%'
                    OR fm.state_key LIKE 'project.%'
                    OR fm.state_key LIKE 'constraint.%'
                    OR (
                      fm.memory_type = 'preference'
                      AND fm.state_key LIKE 'safety.%'
                    )
                  )
                ORDER BY
                  CASE
                    WHEN fm.state_key LIKE 'identity.%' THEN 1
                    WHEN fm.state_key LIKE 'role.%' THEN 2
                    WHEN fm.state_key LIKE 'goal.%' THEN 3
                    WHEN fm.state_key LIKE 'project.%' THEN 4
                    WHEN fm.state_key LIKE 'safety.%' THEN 5
                    WHEN fm.state_key LIKE 'constraint.%' THEN 6
                    ELSE 99
                  END,
                  fm.state_key
                LIMIT :limit
                """
            ),
            {"limit": L0_MAX_ITEMS},
        ).mappings()
        return _bounded_context(rows)

    def l1_context(self, session: Session, *, prefix: str) -> dict[str, dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT fm.state_key, fmv.value_json
                FROM memory_current_state mcs
                JOIN formal_memories fm ON fm.id = mcs.formal_memory_id
                JOIN formal_memory_versions fmv ON fmv.id = mcs.formal_version_id
                WHERE fm.status = 'formal_current'
                  AND fm.current_version_id = mcs.formal_version_id
                  AND fm.current_generation = mcs.effective_generation
                  AND datetime(fm.valid_from) <= datetime('now')
                  AND (fm.valid_to IS NULL OR datetime(fm.valid_to) > datetime('now'))
                  AND fm.state_key LIKE :prefix
                ORDER BY fm.state_key
                """
            ),
            {"prefix": f"{prefix}%"},
        ).mappings()
        return {str(row["state_key"]): _json_dict(row["value_json"]) for row in rows}

    def authorize_l2_evidence(
        self,
        session: Session,
        *,
        formal_memory_id: str,
        formal_version_id: str,
        generation: int,
    ) -> list[dict[str, Any]]:
        current = session.execute(
            text(
                """
                SELECT 1
                FROM current_formal_memory
                WHERE id = :formal_memory_id
                  AND effective_generation = :generation
                  AND current_version_id = :formal_version_id
                """
            ),
            {
                "formal_memory_id": formal_memory_id,
                "generation": generation,
                "formal_version_id": formal_version_id,
            },
        ).first()
        if current is None:
            return []
        rows = session.execute(
            text(
                """
                SELECT evidence_object_id, content_span_id, trajectory_id, support_type
                FROM memory_evidence_refs
                WHERE target_type = 'formal_memory'
                  AND target_id = :formal_memory_id
                  AND target_version_id = :formal_version_id
                ORDER BY support_type
                """
            ),
            {"formal_memory_id": formal_memory_id, "formal_version_id": formal_version_id},
        ).mappings()
        return [dict(row) for row in rows]

    def soft_delete(
        self,
        session: Session,
        *,
        formal_memory_id: str,
        operation_key: str,
        reason: str = "user delete",
        expected_etag: str | None = None,
    ) -> ConfirmationResult:
        request_hash = _request_hash(
            operation_type="soft_delete_memory",
            target_type="formal_memory",
            target_id=formal_memory_id,
            expected_version_id=None,
            expected_generation=None,
            payload={"new_status": "deleted", "reason": reason},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        memory = self._formal_memory(session, formal_memory_id)
        if memory is None:
            raise ValueError("formal memory not found")
        self._require_formal_etag(memory, expected_etag)
        if memory["status"] != "formal_current" or not self._is_serving_formal_memory(
            session, formal_memory_id
        ):
            raise ValueError("formal memory is not current")
        return self._state_transition(
            session,
            memory=memory,
            request_hash=request_hash,
            formal_memory_id=formal_memory_id,
            operation_key=operation_key,
            operation_type="soft_delete_memory",
            new_status="deleted",
            event_type="formal_deleted",
            remove_current=True,
            reason=reason,
        )

    def restore(
        self,
        session: Session,
        *,
        formal_memory_id: str,
        operation_key: str,
        reason: str = "user restore",
        expected_etag: str | None = None,
    ) -> ConfirmationResult:
        request_hash = _request_hash(
            operation_type="restore_memory",
            target_type="formal_memory",
            target_id=formal_memory_id,
            expected_version_id=None,
            expected_generation=None,
            payload={"new_status": "formal_current", "reason": reason},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        memory = self._formal_memory(session, formal_memory_id)
        if memory is None:
            raise ValueError("formal memory not found")
        self._require_formal_etag(memory, expected_etag)
        if memory["status"] == "privacy_erased":
            raise ValueError("privacy_erased formal memory is terminal")
        if memory["status"] != "deleted":
            raise ValueError("only deleted formal memory can be restored")
        return self._state_transition(
            session,
            memory=memory,
            request_hash=request_hash,
            formal_memory_id=formal_memory_id,
            operation_key=operation_key,
            operation_type="restore_memory",
            new_status="formal_current",
            event_type="formal_restored",
            remove_current=False,
            reason=reason,
        )

    def rollback(
        self,
        session: Session,
        *,
        formal_memory_id: str,
        target_version_id: str,
        operation_key: str,
        reason: str = "user rollback",
        expected_etag: str | None = None,
    ) -> ConfirmationResult:
        request_hash = _request_hash(
            operation_type="rollback_memory",
            target_type="formal_memory",
            target_id=formal_memory_id,
            expected_version_id=None,
            expected_generation=None,
            payload={"target_version_id": target_version_id, "reason": reason},
        )
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        memory = self._formal_memory(session, formal_memory_id)
        if memory is None:
            raise ValueError("formal memory not found")
        self._require_formal_etag(memory, expected_etag)
        if memory["status"] == "privacy_erased":
            raise ValueError("privacy_erased formal memory is terminal")
        target = session.execute(
            text(
                """
                SELECT value_json
                FROM formal_memory_versions
                WHERE id = :target_version_id
                  AND formal_memory_id = :formal_memory_id
                """
            ),
            {"target_version_id": target_version_id, "formal_memory_id": formal_memory_id},
        ).mappings().first()
        if target is None:
            raise ValueError("rollback target not found")
        receipt_id = self._insert_receipt(session, operation_key, "rollback_memory", request_hash)
        generation = self._next_generation(session, str(memory["state_key"]))
        version_id = new_id()
        version_no = self._next_formal_version(session, formal_memory_id)
        session.execute(
            text(
                """
                INSERT INTO formal_memory_versions (
                  id, formal_memory_id, version_no, value_json, change_reason,
                  created_by_role, generation, status, source_kind
                )
                VALUES (
                  :id, :formal_memory_id, :version_no, :value_json,
                  :reason, 'system', :generation, 'current', 'rollback'
                )
                """
            ),
            {
                "id": version_id,
                "formal_memory_id": formal_memory_id,
                "version_no": version_no,
                "value_json": json_text(_json_dict(target["value_json"])),
                "generation": generation,
                "reason": reason[:512],
            },
        )
        session.execute(
            text(
                """
                UPDATE formal_memories
                SET status = 'formal_current',
                    current_version_id = :version_id,
                    current_generation = :generation,
                    updated_at = CURRENT_TIMESTAMP,
                    deleted_at = NULL
                WHERE id = :formal_memory_id
                """
            ),
            {
                "version_id": version_id,
                "generation": generation,
                "formal_memory_id": formal_memory_id,
            },
        )
        self._copy_evidence_refs(
            session,
            target_type="formal_memory",
            target_id=formal_memory_id,
            source_version_id=target_version_id,
            target_version_id=version_id,
        )
        self._replace_current_state(
            session, str(memory["state_key"]), formal_memory_id, version_id, generation
        )
        self._insert_generation_event(
            session,
            state_key=str(memory["state_key"]),
            generation=generation,
            event_type="formal_rolled_back",
            receipt_id=receipt_id,
            reason=reason,
        )
        result = ConfirmationResult(
            receipt_id, "", formal_memory_id, version_id, generation, "completed"
        )
        self._complete_receipt(session, receipt_id, result)
        return result

    def _confirm_request(
        self,
        session: Session,
        request: RowMapping,
        candidate: RowMapping,
        receipt_id: str,
        edited_value: dict[str, Any] | None,
    ) -> ConfirmationResult:
        request_id = str(request["id"])
        if candidate["current_version_id"] != request["candidate_version_id"]:
            raise StaleConfirmationError("candidate changed after request")
        if not self._request_hash_still_matches(session, request):
            raise StaleConfirmationError("confirmation request is stale or closed")
        candidate_value = self._candidate_version(session, str(request["candidate_version_id"]))
        final_value = (
            edited_value
            if edited_value is not None
            else _json_dict(candidate_value["value_json"])
        )
        current = self._current_formal_memory_for_state(
            session, str(candidate["state_key"])
        )
        formal_id = str(current["id"]) if current is not None else new_id()
        version_id = new_id()
        generation = self._next_generation(session, str(candidate["state_key"]))
        decision_id = new_id()
        if current is None:
            session.execute(
                text(
                    """
                    INSERT INTO formal_memories (
                      id, memory_type, state_key, status, current_version_id,
                      current_generation, sensitivity_level, confidence, valid_from, valid_to,
                      origin_kind, origin_candidate_id, origin_decision_id
                    )
                    VALUES (
                      :id, :memory_type, :state_key, 'formal_current', :current_version_id,
                      :current_generation, :sensitivity_level, :confidence,
                      CURRENT_TIMESTAMP, NULL, :origin_kind, :candidate_id, :decision_id
                    )
                    """
                ),
                {
                    "id": formal_id,
                    "memory_type": candidate["memory_type"],
                    "state_key": candidate["state_key"],
                    "current_version_id": version_id,
                    "current_generation": generation,
                    "sensitivity_level": candidate["sensitivity_level"],
                    "confidence": candidate["confidence"],
                    "origin_kind": (
                        "edited_confirmed_candidate"
                        if edited_value is not None
                        else "confirmed_candidate"
                    ),
                    "candidate_id": candidate["id"],
                    "decision_id": decision_id,
                },
            )
        else:
            session.execute(
                text(
                    """
                    UPDATE formal_memories
                    SET memory_type = :memory_type,
                        current_version_id = :current_version_id,
                        current_generation = :current_generation,
                        sensitivity_level = :sensitivity_level,
                        confidence = :confidence,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {
                    "id": formal_id,
                    "memory_type": candidate["memory_type"],
                    "current_version_id": version_id,
                    "current_generation": generation,
                    "sensitivity_level": candidate["sensitivity_level"],
                    "confidence": candidate["confidence"],
                },
            )
        session.execute(
            text(
                """
                INSERT INTO formal_memory_versions (
                  id, formal_memory_id, version_no, value_json, change_reason,
                  created_by_role, generation, status, source_kind,
                  source_candidate_id, source_decision_id
                )
                VALUES (
                  :id, :formal_memory_id, :version_no, :value_json,
                  'candidate confirmed', 'user', :generation, 'current', :source_kind,
                  :candidate_id, :decision_id
                )
                """
            ),
            {
                "id": version_id,
                "formal_memory_id": formal_id,
                "version_no": self._next_formal_version(session, formal_id),
                "value_json": json_text(final_value),
                "generation": generation,
                "source_kind": (
                    "edited_confirmed_candidate"
                    if edited_value is not None
                    else "confirmed_candidate"
                ),
                "candidate_id": candidate["id"],
                "decision_id": decision_id,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO memory_confirmation_decisions (
                  id, request_id, decision, final_value_json,
                  formal_memory_id, formal_version_id, generation
                )
                VALUES (
                  :id, :request_id, :decision, :final_value_json,
                  :formal_memory_id, :formal_version_id, :generation
                )
                """
            ),
            {
                "id": decision_id,
                "request_id": request_id,
                "decision": "edited_confirmed" if edited_value is not None else "confirmed",
                "final_value_json": json_text(final_value),
                "formal_memory_id": formal_id,
                "formal_version_id": version_id,
                "generation": generation,
            },
        )
        self._replace_current_state(
            session, str(candidate["state_key"]), formal_id, version_id, generation
        )
        session.execute(
            text("UPDATE memory_confirmation_requests SET status = 'confirmed' WHERE id = :id"),
            {"id": request_id},
        )
        session.execute(
            text("UPDATE memory_candidates SET status = 'confirmed' WHERE id = :id"),
            {"id": candidate["id"]},
        )
        session.execute(
            text(
                """
                INSERT INTO memory_evidence_refs (
                  target_type, target_id, evidence_object_id, content_span_id,
                  trajectory_id, support_type, target_version_id
                )
                SELECT 'formal_memory', :formal_id, evidence_object_id, content_span_id,
                       trajectory_id, support_type, :version_id
                FROM memory_evidence_refs
                WHERE target_type = 'memory_candidate'
                  AND target_id = :candidate_id
                  AND target_version_id = :candidate_version_id
                """
            ),
            {
                "formal_id": formal_id,
                "version_id": version_id,
                "candidate_id": candidate["id"],
                "candidate_version_id": request["candidate_version_id"],
            },
        )
        self._insert_generation_event(
            session,
            state_key=str(candidate["state_key"]),
            generation=generation,
            event_type="candidate_confirmed",
            receipt_id=receipt_id,
            reason="candidate confirmed by user",
        )
        return ConfirmationResult(
            receipt_id, decision_id, formal_id, version_id, generation, "completed"
        )

    def _state_transition(
        self,
        session: Session,
        *,
        memory: RowMapping,
        request_hash: str,
        formal_memory_id: str,
        operation_key: str,
        operation_type: str,
        new_status: str,
        event_type: str,
        remove_current: bool,
        reason: str,
    ) -> ConfirmationResult:
        existing = self._receipt(session, operation_key, request_hash=request_hash)
        if existing is not None:
            return self._result_from_receipt(existing)
        if memory["status"] == "privacy_erased":
            raise ValueError("privacy_erased formal memory is terminal")
        if new_status == "formal_current" and self._serving_state_exists(
            session, str(memory["state_key"]), formal_memory_id
        ):
            raise ValueError("restore conflicts with existing current memory")
        receipt_id = self._insert_receipt(session, operation_key, operation_type, request_hash)
        generation = self._next_generation(session, str(memory["state_key"]))
        current_version_id = str(memory["current_version_id"])
        if remove_current:
            transition_version_id = current_version_id
        else:
            current_value = session.execute(
                text(
                    """
                    SELECT value_json
                    FROM formal_memory_versions
                    WHERE id = :version_id
                      AND formal_memory_id = :formal_memory_id
                    """
                ),
                {"version_id": current_version_id, "formal_memory_id": formal_memory_id},
            ).mappings().one()
            transition_version_id = new_id()
            session.execute(
                text(
                    """
                    INSERT INTO formal_memory_versions (
                      id, formal_memory_id, version_no, value_json, change_reason,
                      created_by_role, generation, status, source_kind
                    )
                    VALUES (
                      :id, :formal_memory_id, :version_no, :value_json,
                      :change_reason, 'system', :generation, 'current', 'restore'
                    )
                    """
                ),
                {
                    "id": transition_version_id,
                    "formal_memory_id": formal_memory_id,
                    "version_no": self._next_formal_version(session, formal_memory_id),
                    "value_json": json_text(_json_dict(current_value["value_json"])),
                    "change_reason": reason[:512],
                    "generation": generation,
                },
            )
            self._copy_evidence_refs(
                session,
                target_type="formal_memory",
                target_id=formal_memory_id,
                source_version_id=current_version_id,
                target_version_id=transition_version_id,
            )
        session.execute(
            text(
                """
                UPDATE formal_memories
                SET status = :status,
                    current_version_id = :current_version_id,
                    current_generation = :generation,
                    updated_at = CURRENT_TIMESTAMP,
                    deleted_at = CASE WHEN :status = 'deleted' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id = :formal_memory_id
                """
            ),
            {
                "status": new_status,
                "current_version_id": transition_version_id,
                "generation": generation,
                "formal_memory_id": formal_memory_id,
            },
        )
        if remove_current:
            session.execute(
                text("DELETE FROM memory_current_state WHERE formal_memory_id = :id"),
                {"id": formal_memory_id},
            )
        else:
            self._replace_current_state(
                session,
                str(memory["state_key"]),
                formal_memory_id,
                transition_version_id,
                generation,
            )
        self._insert_generation_event(
            session,
            state_key=str(memory["state_key"]),
            generation=generation,
            event_type=event_type,
            receipt_id=receipt_id,
            reason=reason,
        )
        result = ConfirmationResult(
            receipt_id,
            "",
            formal_memory_id,
            transition_version_id,
            generation,
            "completed",
        )
        self._complete_receipt(session, receipt_id, result)
        return result

    def _insert_evidence_refs(
        self,
        session: Session,
        *,
        target_type: str,
        target_id: str,
        target_version_id: str,
        refs: list[dict[str, Any]],
    ) -> None:
        for ref in refs:
            session.execute(
                text(
                    """
                    INSERT INTO memory_evidence_refs (
                      target_type, target_id, evidence_object_id, content_span_id,
                      trajectory_id, support_type, target_version_id
                    )
                    VALUES (
                      :target_type, :target_id, :evidence_object_id, :content_span_id,
                      :trajectory_id, :support_type, :target_version_id
                    )
                    """
                ),
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_version_id": target_version_id,
                    "evidence_object_id": ref.get("evidence_object_id"),
                    "content_span_id": ref.get("content_span_id"),
                    "trajectory_id": ref.get("trajectory_id"),
                    "support_type": ref.get("support_type", "supporting"),
                },
            )

    def _copy_evidence_refs(
        self,
        session: Session,
        *,
        target_type: str,
        target_id: str,
        source_version_id: str,
        target_version_id: str,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO memory_evidence_refs (
                  target_type, target_id, target_version_id, evidence_object_id,
                  content_span_id, trajectory_id, support_type
                )
                SELECT target_type, target_id, :target_version_id, evidence_object_id,
                       content_span_id, trajectory_id, support_type
                FROM memory_evidence_refs
                WHERE target_type = :target_type
                  AND target_id = :target_id
                  AND target_version_id = :source_version_id
                """
            ),
            {
                "target_type": target_type,
                "target_id": target_id,
                "source_version_id": source_version_id,
                "target_version_id": target_version_id,
            },
        )

    def _replace_current_state(
        self,
        session: Session,
        state_key: str,
        formal_id: str,
        version_id: str,
        generation: int,
    ) -> None:
        session.execute(
            text("DELETE FROM memory_current_state WHERE state_key = :state_key"),
            {"state_key": state_key},
        )
        session.execute(
            text(
                """
                INSERT INTO memory_current_state (
                  scope, state_key, formal_memory_id, formal_version_id, effective_generation
                )
                VALUES ('default', :state_key, :formal_id, :version_id, :generation)
                """
            ),
            {
                "state_key": state_key,
                "formal_id": formal_id,
                "version_id": version_id,
                "generation": generation,
            },
        )

    def _insert_generation_event(
        self,
        session: Session,
        *,
        state_key: str,
        generation: int,
        event_type: str,
        receipt_id: str,
        reason: str = "",
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO memory_generation_events (
                  id, state_key, generation, event_type, receipt_id, reason
                )
                VALUES (:id, :state_key, :generation, :event_type, :receipt_id, :reason)
                """
            ),
            {
                "id": new_id(),
                "state_key": state_key,
                "generation": generation,
                "event_type": event_type,
                "receipt_id": receipt_id,
                "reason": reason[:512],
            },
        )

    def _insert_receipt(
        self,
        session: Session,
        operation_key: str,
        operation_type: str,
        request_hash: str | None = None,
    ) -> str:
        bound_request_hash = request_hash or _request_hash(
            operation_type=operation_type,
            target_type="operation_key",
            target_id=operation_key,
            expected_version_id=None,
            expected_generation=None,
            payload={},
        )
        receipt_id = new_id()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO memory_operation_receipts (
                      id, operation_key, operation_type, request_hash, status, result_json
                    )
                    VALUES (
                      :id, :operation_key, :operation_type, :request_hash, 'started', '{}'
                    )
                    """
                ),
                {
                    "id": receipt_id,
                    "operation_key": operation_key,
                    "operation_type": operation_type,
                    "request_hash": bound_request_hash,
                },
            )
        except IntegrityError:
            existing = self._receipt(
                session, operation_key, request_hash=bound_request_hash
            )
            if existing is None:
                raise
            return str(existing["id"])
        return receipt_id

    def _complete_receipt(
        self, session: Session, receipt_id: str, result: ConfirmationResult
    ) -> None:
        session.execute(
            text(
                """
                UPDATE memory_operation_receipts
                SET status = :status, result_json = :result_json, completed_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {
                "id": receipt_id,
                "status": result.status,
                "result_json": json_text(
                    {
                        "receipt_id": result.receipt_id,
                        "decision_id": result.decision_id,
                        "formal_memory_id": result.formal_memory_id,
                        "formal_version_id": result.formal_version_id,
                        "generation": result.generation,
                        "status": result.status,
                    }
                ),
            },
        )

    def _fail_receipt(self, session: Session, receipt_id: str) -> None:
        session.execute(
            text(
                """
                UPDATE memory_operation_receipts
                SET status = 'failed', completed_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": receipt_id},
        )

    def _receipt(
        self, session: Session, operation_key: str, *, request_hash: str | None = None
    ) -> RowMapping | None:
        row = session.execute(
            text(
                """
                SELECT id, result_json, status, request_hash
                FROM memory_operation_receipts
                WHERE operation_key = :operation_key
                """
            ),
            {"operation_key": operation_key},
        ).mappings().first()
        if row is None:
            return None
        if request_hash is not None and row["request_hash"] != request_hash:
            raise OperationConflictError("idempotency key was reused with different payload")
        return row

    def _request_hash_still_matches(self, session: Session, request: RowMapping) -> bool:
        current_hash = self._candidate_value_hash(session, str(request["candidate_version_id"]))
        candidate = self._candidate(session, str(request["candidate_id"]))
        return bool(
            candidate is not None
            and candidate["current_version_id"] == request["candidate_version_id"]
            and candidate["status"] in {"pending_confirmation", "edited"}
            and request["status"] == "pending"
            and
            current_hash == request["proposed_value_hash"]
            and _coerce_datetime(request["expires_at"]) > _utc_now()
        )

    def _serving_state_exists(
        self, session: Session, state_key: str, excluding_formal_memory_id: str
    ) -> bool:
        return (
            session.execute(
                text(
                    """
                    SELECT 1
                    FROM current_formal_memory
                    WHERE state_key = :state_key
                      AND id <> :formal_memory_id
                    LIMIT 1
                    """
                ),
                {
                    "state_key": state_key,
                    "formal_memory_id": excluding_formal_memory_id,
                },
            ).first()
            is not None
        )

    def _result_from_receipt(self, row: RowMapping) -> ConfirmationResult:
        result = _json_dict(row["result_json"])
        return ConfirmationResult(
            receipt_id=str(row["id"]),
            decision_id=str(result.get("decision_id") or ""),
            formal_memory_id=result.get("formal_memory_id"),
            formal_version_id=result.get("formal_version_id"),
            generation=result.get("generation"),
            status=str(result.get("status") or row["status"]),
        )

    def _candidate(self, session: Session, candidate_id: str) -> RowMapping | None:
        return session.execute(
            text("SELECT * FROM memory_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).mappings().first()

    def _candidate_version(self, session: Session, version_id: str) -> RowMapping:
        row = session.execute(
            text("SELECT * FROM memory_candidate_versions WHERE id = :id"),
            {"id": version_id},
        ).mappings().one()
        return row

    def _pending_request(self, session: Session, request_id: str) -> RowMapping | None:
        return session.execute(
            text(
                """
                SELECT *
                FROM memory_confirmation_requests
                WHERE id = :id
                  AND status = 'pending'
                """
            ),
            {"id": request_id},
        ).mappings().first()

    def _request(self, session: Session, request_id: str) -> RowMapping | None:
        return session.execute(
            text("SELECT * FROM memory_confirmation_requests WHERE id = :id"),
            {"id": request_id},
        ).mappings().first()

    def _formal_memory(self, session: Session, formal_memory_id: str) -> RowMapping | None:
        return session.execute(
            text("SELECT * FROM formal_memories WHERE id = :id"),
            {"id": formal_memory_id},
        ).mappings().first()

    def formal_etag(self, session: Session, formal_memory_id: str) -> str | None:
        memory = self._formal_memory(session, formal_memory_id)
        return None if memory is None else formal_memory_etag(memory)

    def candidate_etag(self, session: Session, candidate_id: str) -> str | None:
        candidate = self._candidate(session, candidate_id)
        return None if candidate is None else _candidate_etag_value(candidate)

    def pending_request_id(self, session: Session, candidate_id: str) -> str | None:
        row = session.execute(
            text(
                """
                SELECT id FROM memory_confirmation_requests
                WHERE candidate_id = :candidate_id AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"candidate_id": candidate_id},
        ).first()
        return None if row is None else str(row[0])

    def _require_formal_etag(
        self, memory: RowMapping, expected_etag: str | None
    ) -> None:
        if expected_etag is not None and formal_memory_etag(memory) != expected_etag:
            raise StaleMemoryStateError("formal memory lifecycle state changed")

    def _current_formal_memory_for_state(
        self, session: Session, state_key: str
    ) -> RowMapping | None:
        return session.execute(
            text("SELECT * FROM current_formal_memory WHERE state_key = :state_key"),
            {"state_key": state_key},
        ).mappings().first()

    def _is_serving_formal_memory(self, session: Session, formal_memory_id: str) -> bool:
        return (
            session.execute(
                text("SELECT 1 FROM current_formal_memory WHERE id = :formal_memory_id"),
                {"formal_memory_id": formal_memory_id},
            ).first()
            is not None
        )

    def _candidate_value_hash(self, session: Session, version_id: str) -> str:
        version = self._candidate_version(session, version_id)
        return sha256_json(_json_dict(version["value_json"]))

    def _next_candidate_version(self, session: Session, candidate_id: str) -> int:
        return int(
            session.execute(
                text(
                    """
                    SELECT coalesce(max(version_no), 0) + 1
                    FROM memory_candidate_versions
                    WHERE candidate_id = :candidate_id
                    """
                ),
                {"candidate_id": candidate_id},
            ).scalar_one()
        )

    def _next_formal_version(self, session: Session, formal_memory_id: str) -> int:
        return int(
            session.execute(
                text(
                    """
                    SELECT coalesce(max(version_no), 0) + 1
                    FROM formal_memory_versions
                    WHERE formal_memory_id = :formal_memory_id
                    """
                ),
                {"formal_memory_id": formal_memory_id},
            ).scalar_one()
        )

    def _next_generation(self, session: Session, state_key: str) -> int:
        return int(
            session.execute(
                text(
                    """
                    SELECT coalesce(max(generation), 0) + 1
                    FROM memory_generation_events
                    WHERE state_key = :state_key
                    """
                ),
                {"state_key": state_key},
            ).scalar_one()
        )


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise TypeError("expected JSON object")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_interval(valid_from: datetime | None, valid_to: datetime | None) -> None:
    if (
        valid_from is not None
        and valid_to is not None
        and _coerce_datetime(valid_from) > _coerce_datetime(valid_to)
    ):
        raise ValueError("valid_from must be earlier than or equal to valid_to")


def _confidence_explanation(confidence: float) -> dict[str, Any]:
    explicitness = round(min(1.0, confidence + 0.15), 3)
    consistency = round(confidence, 3)
    return {
        "score": round(confidence, 3),
        "factors": {
            "explicitness": explicitness,
            "evidence_count": 0,
            "consistency": consistency,
            "timeliness": 1.0,
        },
        "summary": "置信度由显式程度、证据数量、一致性和时效性综合得出。",
    }


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise TypeError("expected datetime value")


def _memory_payload(memory: MemoryValue) -> dict[str, Any]:
    return {
        "memory_type": memory.memory_type,
        "state_key": memory.state_key,
        "value": memory.value,
        "sensitivity_level": memory.sensitivity_level,
        "confidence": memory.confidence,
        "valid_from": _datetime_payload(memory.valid_from),
        "valid_to": _datetime_payload(memory.valid_to),
    }


def _datetime_payload(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _coerce_datetime(value).isoformat()


def _request_hash(
    *,
    operation_type: str,
    target_type: str,
    target_id: str,
    expected_version_id: str | None,
    expected_generation: int | None,
    payload: dict[str, Any],
) -> str:
    return sha256_json(
        {
            "operation_type": operation_type,
            "target_type": target_type,
            "target_id": target_id,
            "expected_version_id": expected_version_id,
            "expected_generation": expected_generation,
            "payload": payload,
        }
    )


def formal_memory_etag(item: RowMapping | dict[str, Any]) -> str:
    return sha256_json(
        {
            "id": str(item["id"]),
            "current_version_id": str(item["current_version_id"]),
            "current_generation": int(item["current_generation"]),
            "status": str(item["status"]),
        }
    )


def _candidate_etag_value(item: RowMapping | dict[str, Any]) -> str:
    return sha256_json(
        {
            "id": str(item["id"]),
            "current_version_id": str(item["current_version_id"]),
            "status": str(item["status"]),
        }
    )


def _bounded_context(rows: Iterable[RowMapping]) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for row in rows:
        state_key = str(row["state_key"])
        if not _is_l0_allowed(state_key, str(row.get("memory_type", ""))):
            continue
        context[state_key] = _json_dict(row["value_json"])
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > L0_MAX_SERIALIZED_BYTES:
            context.pop(state_key)
            break
        if len(context) >= L0_MAX_ITEMS:
            break
    return context


def _is_l0_allowed(state_key: str, memory_type: str) -> bool:
    if state_key.startswith(L0_ALLOWED_PREFIXES):
        return True
    return memory_type == "preference" and state_key.startswith("safety.")
