from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.conclusions.applicability import ConclusionApplicabilityService
from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.memory.repository import MemoryCandidateInput, MemoryRepository, MemoryValue


@dataclass(frozen=True, slots=True)
class PersonalUpdate:
    memory_type: str
    state_key: str
    value: dict[str, Any]
    source_kind: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_sensitivity: str = "persistent"
    temporal_change: bool = False
    quoted: bool = False
    hypothetical: bool = False
    joke: bool = False
    inferred: bool = False
    rationale: str = "personal update"
    confidence: float = 1.0


class PersonalUpdateService:
    def __init__(self, repository: MemoryRepository | None = None) -> None:
        self.repository = repository or MemoryRepository()

    def apply(
        self,
        session: Session,
        update: PersonalUpdate,
        *,
        operation_key: str,
        owner_user_id: str = "default",
    ) -> dict[str, Any]:
        payload = _payload(update)
        request_hash = sha256_json({"operation": "personal_update", "payload": payload})
        existing = self._receipt(session, operation_key, request_hash)
        if existing is not None:
            return _json_object(existing["result_json"])

        if _requires_review(update):
            self._notify_conclusions(session, owner_user_id, update, certain=False)
            result = self._propose(session, update, owner_user_id=owner_user_id)
            self._complete_receipt(session, operation_key, request_hash, result)
            return result

        current = self._current(session, update.state_key)
        if current is not None and _same_value(current["value_json"], update.value):
            result = {
                "disposition": "unchanged",
                "state_key": update.state_key,
                "formal_memory_id": str(current["id"]),
            }
            self._complete_receipt(session, operation_key, request_hash, result)
            return result

        if current is not None and not update.temporal_change:
            self._notify_conclusions(session, owner_user_id, update, certain=False)
            result = self._propose(
                session,
                update,
                conflict=True,
                current_value=_json_object(current["value_json"]),
                owner_user_id=owner_user_id,
            )
            self._complete_receipt(session, operation_key, request_hash, result)
            return result

        committed = self.repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type=update.memory_type,
                state_key=update.state_key,
                value=update.value,
                confidence=update.confidence,
                valid_from=update.valid_from,
                valid_to=update.valid_to,
                time_sensitivity=update.time_sensitivity,
            ),
            operation_key=f"personal:{operation_key}",
            evidence_refs=_formal_evidence_refs(update.evidence_refs),
        )
        result = {
            "disposition": "auto_updated" if current is not None else "auto_confirmed",
            "state_key": update.state_key,
            "formal_memory_id": committed.formal_memory_id,
            "formal_version_id": committed.formal_version_id,
            "generation": committed.generation,
            "status": committed.status,
        }
        self._record_formal_update(session, owner_user_id, update)
        effective = update.valid_from
        if effective is None or effective.replace(tzinfo=effective.tzinfo or UTC) <= datetime.now(
            UTC
        ):
            self._notify_conclusions(session, owner_user_id, update, certain=True)
        else:
            ConclusionApplicabilityService().fact_changed(
                session,
                owner_user_id,
                update.state_key,
                update.value,
                certain=False,
                evidence={
                    "future_fact": True,
                    "effective_at": effective.isoformat(),
                    "source_kind": update.source_kind,
                    "evidence_refs": update.evidence_refs,
                },
            )
        self._complete_receipt(session, operation_key, request_hash, result)
        return result

    def _notify_conclusions(
        self, session: Session, owner: str, update: PersonalUpdate, *, certain: bool
    ) -> None:
        ConclusionApplicabilityService().fact_changed(
            session,
            owner,
            update.state_key,
            update.value,
            certain=certain,
            evidence={
                "source_kind": update.source_kind,
                "evidence_refs": update.evidence_refs,
                "rationale": update.rationale,
                "value": update.value,
            },
        )

    def list_triage(self, session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
        conflicts = self.list_conflicts(session, limit=limit)
        conflict_candidate_ids = {str(item["candidate_id"]) for item in conflicts}
        rows = session.execute(
            text(
                """
                SELECT mc.id, mc.state_key, mc.memory_type, mc.status, mc.current_version_id,
                       mcv.value_json, mc.rationale, mc.source_kind, mc.created_at
                FROM memory_candidates mc
                JOIN memory_candidate_versions mcv ON mcv.id = mc.current_version_id
                WHERE mc.status IN ('pending_confirmation', 'edited')
                ORDER BY mc.updated_at DESC, mc.created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": max(1, min(limit, 500))},
        ).mappings()
        items = [
            {
                "id": str(row["id"]),
                "candidate_id": str(row["id"]),
                "state_key": str(row["state_key"]),
                "memory_type": str(row["memory_type"]),
                "value": _json_object(row["value_json"]),
                "reason": str(row["rationale"]),
                "source_kind": str(row["source_kind"]),
                "status": str(row["status"]),
                "triage_status": "pending",
                "conflict": str(row["id"]) in conflict_candidate_ids,
            }
            for row in rows
            if str(row["id"]) not in conflict_candidate_ids
        ]
        return conflicts + items

    def list_conflicts(self, session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.repository.list_conflicts(session, status="pending", limit=limit)
        for item in rows:
            item["triage_status"] = item.get("resolution") or "pending"
            item["candidate_id"] = str(item["candidate_id"])
        return rows

    def context_prompts(
        self,
        session: Session,
        *,
        query: str,
        owner_user_id: str = "default",
        limit: int = 20,
        include_deferred: bool = False,
    ) -> list[dict[str, Any]]:
        """Return only unresolved prompts that are relevant to the current query."""
        normalized = query.casefold()
        prompts: list[dict[str, Any]] = []
        conflict_rows = session.execute(
            text(
                """
                SELECT pc.id, pc.state_key, pc.candidate_json, pc.existing_json, pc.status,
                       c.candidate_id,
                       mc.source_kind AS candidate_source, mc.rationale AS candidate_reason,
                       fmv.source_kind AS existing_source
                FROM personal_conflicts pc
                JOIN memory_conflicts c ON c.id = pc.id
                JOIN memory_candidates mc ON mc.id = c.candidate_id
                LEFT JOIN current_formal_memory fm ON fm.state_key = pc.state_key
                LEFT JOIN formal_memory_versions fmv ON fmv.id = fm.current_version_id
                WHERE pc.owner_user_id = :owner
                  AND pc.status IN ('pending', 'deferred')
                  AND (:include_deferred = 1 OR pc.status = 'pending')
                ORDER BY pc.created_at DESC
                LIMIT :limit
                """
            ),
            {
                "owner": owner_user_id,
                "limit": max(1, min(limit, 100)),
                "include_deferred": int(include_deferred),
            },
        ).mappings()
        for row in conflict_rows:
            candidate = _json_object(row["candidate_json"])
            existing = _json_object(row["existing_json"])
            if not _prompt_matches(normalized, str(row["state_key"]), candidate, existing):
                continue
            prompts.append(
                {
                    "id": str(row["id"]),
                    "kind": "conflict",
                    "status": str(row["status"]),
                    "state_key": str(row["state_key"]),
                    "reason": "当前问题涉及相互冲突的个人信息，请选择、补充或稍后处理。",
                    "candidate": candidate,
                    "existing": existing,
                    "candidate_source": str(row["candidate_source"]),
                    "existing_source": str(row["existing_source"])
                    if row["existing_source"] is not None
                    else None,
                    "conflict_id": str(row["id"]),
                    "candidate_etag": self.repository.candidate_etag(
                        session, str(row["candidate_id"])
                    ),
                    "actions": ["confirm", "supplement", "defer", "skip"],
                }
            )
        missing_rows = session.execute(
            text(
                """
                SELECT id, prompt_kind, state_key, reason, payload_json, status
                FROM personal_prompts
                WHERE owner_user_id = :owner
                  AND status IN ('pending', 'deferred')
                  AND (:include_deferred = 1 OR status = 'pending')
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {
                "owner": owner_user_id,
                "limit": max(1, min(limit, 100)),
                "include_deferred": int(include_deferred),
            },
        ).mappings()
        for row in missing_rows:
            payload = _json_object(row["payload_json"])
            if str(row["prompt_kind"]) != "missing":
                continue
            if not _prompt_matches(normalized, str(row["state_key"] or ""), payload):
                continue
            prompts.append(
                {
                    "id": str(row["id"]),
                    "kind": "missing",
                    "status": str(row["status"]),
                    "state_key": str(row["state_key"] or ""),
                    "reason": str(row["reason"]),
                    "payload": payload,
                    "actions": ["supplement", "defer", "skip"],
                }
            )
        return prompts[: max(1, min(limit, 100))]

    def create_missing_prompt(
        self,
        session: Session,
        *,
        owner_user_id: str,
        query: str,
        reason: str,
    ) -> dict[str, Any]:
        state_key = "context.missing." + sha256_json({"query": query})[:24]
        existing = session.execute(
            text(
                """
                SELECT id, reason, payload_json FROM personal_prompts
                WHERE owner_user_id=:owner AND prompt_kind='missing' AND state_key=:state_key
                  AND status='pending'
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"owner": owner_user_id, "state_key": state_key},
        ).mappings().first()
        if existing is not None:
            return {
                "id": str(existing["id"]),
                "kind": "missing",
                "status": "pending",
                "state_key": state_key,
                "reason": str(existing["reason"]),
                "payload": _json_object(existing["payload_json"]),
                "actions": ["supplement", "defer", "skip"],
            }
        prompt_id = new_id()
        payload = {"query": query, "reason": reason}
        session.execute(
            text(
                """
                INSERT INTO personal_prompts
                  (id, owner_user_id, prompt_kind, state_key, reason, payload_json)
                VALUES (:id, :owner, 'missing', :state_key, :reason, :payload)
                """
            ),
            {
                "id": prompt_id,
                "owner": owner_user_id,
                "state_key": state_key,
                "reason": reason,
                "payload": json_text(payload),
            },
        )
        return {
            "id": prompt_id,
            "kind": "missing",
            "status": "pending",
            "state_key": state_key,
            "reason": reason,
            "payload": payload,
            "actions": ["supplement", "defer", "skip"],
        }

    def decide_missing_prompt(
        self, session: Session, prompt_id: str, decision: str
    ) -> dict[str, Any]:
        if decision not in {"defer", "skip"}:
            raise ValueError("unsupported missing prompt decision")
        result = session.execute(
            text(
                """
                UPDATE personal_prompts
                SET status=:status, resolved_at=CURRENT_TIMESTAMP
                WHERE id=:id AND prompt_kind='missing' AND status='pending'
                """
            ),
            {"id": prompt_id, "status": decision + "ed"},
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise ValueError("prompt not found")
        status = "skipped" if decision == "skip" else "deferred"
        return {"prompt_id": prompt_id, "status": status}

    def defer_conflict(
        self, session: Session, conflict_id: str, *, owner_user_id: str = "default"
    ) -> dict[str, Any]:
        row = session.execute(
            text("SELECT id FROM memory_conflicts WHERE id = :id AND status = 'pending'"),
            {"id": conflict_id},
        ).first()
        if row is None:
            raise ValueError("conflict not found")
        session.execute(
            text(
                """
                UPDATE memory_conflicts
                SET resolution = 'deferred'
                WHERE id = :id AND status = 'pending'
                """
            ),
            {"id": conflict_id},
        )
        session.execute(
            text(
                """
                UPDATE personal_conflicts
                SET status = 'deferred'
                WHERE id = :id AND owner_user_id = :owner
                """
            ),
            {"id": conflict_id, "owner": owner_user_id},
        )
        return {"conflict_id": conflict_id, "status": "deferred"}

    def skip_conflict(
        self, session: Session, conflict_id: str, *, owner_user_id: str = "default"
    ) -> dict[str, Any]:
        row = session.execute(
            text("SELECT id FROM memory_conflicts WHERE id = :id AND status = 'pending'"),
            {"id": conflict_id},
        ).first()
        if row is None:
            raise ValueError("conflict not found")
        session.execute(
            text(
                """
                UPDATE memory_conflicts
                SET resolution = 'skipped'
                WHERE id = :id AND status = 'pending'
                """
            ),
            {"id": conflict_id},
        )
        session.execute(
            text(
                """
                UPDATE personal_conflicts
                SET status = 'skipped'
                WHERE id = :id AND owner_user_id = :owner
                """
            ),
            {"id": conflict_id, "owner": owner_user_id},
        )
        return {"conflict_id": conflict_id, "status": "skipped"}

    def resolve_conflict(
        self,
        session: Session,
        conflict_id: str,
        resolution: str,
        *,
        owner_user_id: str = "default",
    ) -> None:
        if resolution == "confirmed":
            fact = (
                session.execute(
                    text("""
                SELECT c.state_key, v.value_json FROM memory_conflicts c
                JOIN memory_candidates m ON m.id=c.candidate_id
                JOIN memory_candidate_versions v ON v.id=m.current_version_id
                WHERE c.id=:id
            """),
                    {"id": conflict_id},
                )
                .mappings()
                .first()
            )
            if fact is not None:
                ConclusionApplicabilityService().fact_changed(
                    session,
                    owner_user_id,
                    str(fact["state_key"]),
                    _json_object(fact["value_json"]),
                    certain=True,
                    evidence={"conflict_id": conflict_id, "source_kind": "user_confirmed"},
                )
        session.execute(
            text(
                """
                UPDATE memory_conflicts
                SET status = 'resolved', resolution = :resolution,
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = :id AND status = 'pending'
                """
            ),
            {"id": conflict_id, "resolution": resolution},
        )
        session.execute(
            text(
                """
                UPDATE personal_conflicts
                SET status = :resolution, resolved_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :owner
                """
            ),
            {"id": conflict_id, "resolution": resolution, "owner": owner_user_id},
        )

    def _propose(
        self,
        session: Session,
        update: PersonalUpdate,
        *,
        conflict: bool = False,
        current_value: dict[str, Any] | None = None,
        owner_user_id: str = "default",
    ) -> dict[str, Any]:
        candidate_id = self.repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred" if update.inferred else "explicit_extracted",
                memory_type=update.memory_type,
                state_key=update.state_key,
                proposed_value=update.value,
                rationale=update.rationale,
                source_kind=update.source_kind,
                confidence=update.confidence,
                valid_from=update.valid_from,
                valid_to=update.valid_to,
                time_sensitivity=update.time_sensitivity,
                evidence_refs=_formal_evidence_refs(update.evidence_refs),
            ),
        )
        request_id = self.repository.request_confirmation(
            session,
            candidate_id=candidate_id,
            risk_level="high" if update.inferred or conflict else "medium",
        )
        conflict_row = session.execute(
            text(
                """
                SELECT id FROM memory_conflicts
                WHERE candidate_id = :candidate_id AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"candidate_id": candidate_id},
        ).first()
        result: dict[str, Any] = {
            "disposition": "conflict_pending"
            if conflict_row is not None
            else "pending_confirmation",
            "candidate_id": candidate_id,
            "request_id": request_id,
            "state_key": update.state_key,
        }
        if conflict_row is not None:
            result["conflict_id"] = str(conflict_row[0])
            session.execute(
                text(
                    """
                    INSERT INTO personal_conflicts
                      (id, owner_user_id, state_key, candidate_json, existing_json)
                    VALUES (:id, :owner, :state_key, :candidate, :existing)
                    """
                ),
                {
                    "id": str(conflict_row[0]),
                    "owner": owner_user_id,
                    "state_key": update.state_key,
                    "candidate": json_text(update.value),
                    "existing": json_text(current_value or {}),
                },
            )
        else:
            session.execute(
                text(
                    """
                    INSERT INTO personal_prompts
                      (id, owner_user_id, prompt_kind, state_key, reason, payload_json)
                    VALUES (:id, :owner, 'confirmation', :state_key, :reason, :payload)
                    """
                ),
                {
                    "id": new_id(),
                    "owner": owner_user_id,
                    "state_key": update.state_key,
                    "reason": update.rationale,
                    "payload": json_text(result),
                },
            )
        return result

    def _record_formal_update(
        self, session: Session, owner_user_id: str, update: PersonalUpdate
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO personal_updates
                  (id, owner_user_id, state_key, value_json, source_text, source_kind,
                   valid_from, valid_to, status)
                VALUES (:id, :owner, :state_key, :value, :source_text, :source_kind,
                        :valid_from, :valid_to, 'formal')
                """
            ),
            {
                "id": new_id(),
                "owner": owner_user_id,
                "state_key": update.state_key,
                "value": json_text(update.value),
                "source_text": str(update.value.get("text", update.rationale)),
                "source_kind": update.source_kind,
                "valid_from": update.valid_from,
                "valid_to": update.valid_to,
            },
        )

    def _current(self, session: Session, state_key: str) -> Any:
        return (
            session.execute(
                text(
                    """
                SELECT fm.id, fmv.value_json
                FROM current_formal_memory fm
                JOIN formal_memory_versions fmv ON fmv.id = fm.current_version_id
                WHERE fm.state_key = :state_key
                """
                ),
                {"state_key": state_key},
            )
            .mappings()
            .first()
        )

    def _receipt(self, session: Session, operation_key: str, request_hash: str) -> Any:
        row = (
            session.execute(
                text(
                    """
                SELECT result_json, request_hash FROM memory_operation_receipts
                WHERE operation_key = :operation_key
                """
                ),
                {"operation_key": operation_key},
            )
            .mappings()
            .first()
        )
        if row is not None and row["request_hash"] != request_hash:
            raise ValueError("idempotency key was reused with different payload")
        return row

    def _complete_receipt(
        self, session: Session, operation_key: str, request_hash: str, result: dict[str, Any]
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO memory_operation_receipts
                  (id, operation_key, operation_type, request_hash, status, result_json,
                   completed_at)
                VALUES (:id, :operation_key, 'personal_update', :request_hash, 'completed',
                        :result_json, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": new_id(),
                "operation_key": operation_key,
                "request_hash": request_hash,
                "result_json": json_text(result),
            },
        )


def _requires_review(update: PersonalUpdate) -> bool:
    return (
        update.inferred
        or update.quoted
        or update.hypothetical
        or update.joke
        or update.source_kind not in {"user_explicit", "user", "conversation_explicit"}
    )


def _same_value(raw: object, value: dict[str, Any]) -> bool:
    return _json_object(raw) == value


def _prompt_matches(
    normalized_query: str,
    state_key: str,
    *values: dict[str, Any],
) -> bool:
    if not normalized_query:
        return False
    key_tokens = [token for token in state_key.casefold().replace("_", ".").split(".") if token]
    if any(len(token) > 1 and token in normalized_query for token in key_tokens):
        return True
    aliases = {
        "city": ("城市", "住", "居住", "所在地"),
        "location": ("城市", "住", "居住", "所在地"),
        "job": ("工作", "职业", "职位"),
        "role": ("工作", "职业", "职位"),
        "goal": ("目标", "计划"),
        "preference": ("偏好", "喜欢", "习惯"),
    }
    if any(alias in normalized_query for token in key_tokens for alias in aliases.get(token, ())):
        return True
    for value in values:
        source_query = str(value.get("query", "")).casefold()
        if source_query and (source_query == normalized_query or source_query in normalized_query):
            return True
        text_value = str(value.get("text", "")).casefold()
        if text_value and text_value in normalized_query:
            return True
    return False


def _json_object(raw: object) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _payload(update: PersonalUpdate) -> dict[str, Any]:
    return {
        "memory_type": update.memory_type,
        "state_key": update.state_key,
        "value": update.value,
        "source_kind": update.source_kind,
        "evidence_refs": update.evidence_refs,
        "valid_from": update.valid_from.isoformat() if update.valid_from else None,
        "valid_to": update.valid_to.isoformat() if update.valid_to else None,
        "time_sensitivity": update.time_sensitivity,
        "temporal_change": update.temporal_change,
        "quoted": update.quoted,
        "hypothetical": update.hypothetical,
        "joke": update.joke,
        "inferred": update.inferred,
        "rationale": update.rationale,
        "confidence": update.confidence,
    }


def _formal_evidence_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        ref
        for ref in refs
        if any(
            ref.get(key) is not None
            for key in ("evidence_object_id", "content_span_id", "trajectory_id")
        )
    ]
