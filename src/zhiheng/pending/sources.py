"""Per-type readers that turn each pending table into ``PendingItem`` values."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.classification.taxonomy import TaxonomyRepository
from zhiheng.conclusions.repository import ConclusionRepository, relation_etag
from zhiheng.core.ids import sha256_json
from zhiheng.knowledge.classification import (
    UNCLASSIFIED_SQL,
    ClassificationRepository,
    suggested_primary_domain,
)
from zhiheng.memory.personal_updates import PersonalUpdateService
from zhiheng.memory.repository import MemoryRepository
from zhiheng.pending.deferrals import DeferralStore
from zhiheng.pending.inbox import ActionHandler, PendingInbox, PendingItem, instant


def _payload(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value)) if value is not None else {}
    return decoded if isinstance(decoded, dict) else {}


class ConclusionDraftSource:
    type = "conclusion_draft"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        rows = session.execute(
            text(
                """
                SELECT e.id, e.status, e.current_version, e.created_at, v.payload_json
                FROM conclusion_entries e
                JOIN conclusion_versions v ON v.entry_id = e.id AND v.version = e.current_version
                WHERE e.owner_user_id = :owner AND e.status IN ('draft', 'deferred')
                """
            ),
            {"owner": owner},
        ).mappings()
        items = []
        for row in rows:
            payload = _payload(row["payload_json"])
            deferred = str(row["status"]) == "deferred"
            items.append(
                PendingItem(
                    type=self.type,
                    id=str(row["id"]),
                    etag=sha256_json(
                        {
                            "id": str(row["id"]),
                            "version": row["current_version"],
                            "status": row["status"],
                        }
                    ),
                    pending_since=instant(row["created_at"]),
                    actions=("approve", "reject", "restore" if deferred else "defer"),
                    requires_individual_approval=True,
                    deferred=deferred,
                    title=str(payload.get("title") or payload.get("claim") or ""),
                    summary=str(payload.get("claim") or "") or None,
                    detail={"version": row["current_version"], "status": row["status"]},
                )
            )
        return items


class RelationSuggestionSource:
    type = "relation_suggestion"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        rows = session.execute(
            text(
                """
                SELECT r.id, r.kind, r.status, r.left_id, r.right_id,
                       r.left_version, r.right_version,
                       COALESCE(r.created_at, (
                         SELECT MIN(ev.created_at) FROM conclusion_relation_events ev
                         WHERE ev.relation_id = r.id
                       )) AS pending_since,
                       le.status AS left_status, le.current_version AS left_current,
                       re.status AS right_status, re.current_version AS right_current,
                       lv.payload_json AS left_payload, rv.payload_json AS right_payload
                FROM conclusion_relations r
                JOIN conclusion_entries le
                  ON le.id = r.left_id AND le.owner_user_id = r.owner_user_id
                JOIN conclusion_entries re
                  ON re.id = r.right_id AND re.owner_user_id = r.owner_user_id
                JOIN conclusion_versions lv
                  ON lv.entry_id = r.left_id AND lv.version = r.left_version
                JOIN conclusion_versions rv
                  ON rv.entry_id = r.right_id AND rv.version = r.right_version
                WHERE r.owner_user_id = :owner
                  AND r.status IN ('proposed', 'deferred')
                  AND le.status IN ('draft', 'deferred')
                """
            ),
            {"owner": owner},
        ).mappings()
        items = []
        for row in rows:
            deferred = str(row["status"]) == "deferred"
            approvable = (
                row["left_status"] == "draft"
                and row["right_status"] == "formal"
                and int(row["left_current"]) == int(row["left_version"])
                and int(row["right_current"]) == int(row["right_version"])
            )
            left = _payload(row["left_payload"])
            right = _payload(row["right_payload"])
            actions: tuple[str, ...] = ("approve",) if approvable else ()
            actions += ("reject", "restore" if deferred else "defer")
            items.append(
                PendingItem(
                    type=self.type,
                    id=str(row["id"]),
                    etag=relation_etag(
                        str(row["id"]),
                        str(row["status"]),
                        row["left_version"],
                        row["right_version"],
                    ),
                    pending_since=instant(row["pending_since"]),
                    actions=actions,
                    requires_individual_approval=True,
                    deferred=deferred,
                    title=str(left.get("claim") or left.get("title") or ""),
                    summary=str(right.get("claim") or "") or None,
                    detail={
                        "kind": row["kind"],
                        "left_id": row["left_id"],
                        "right_id": row["right_id"],
                        "stale": not approvable,
                    },
                )
            )
        return items


class PersonalConflictSource:
    type = "personal_conflict"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        rows = session.execute(
            text(
                """
                SELECT c.id, c.state_key, c.resolution, c.candidate_id, c.created_at,
                       c.candidate_value_json, c.formal_value_json,
                       mc.status AS candidate_status
                FROM memory_conflicts c
                LEFT JOIN memory_candidates mc ON mc.id = c.candidate_id
                WHERE c.status = 'pending'
                """
            )
        ).mappings()
        repository = MemoryRepository()
        items = []
        for row in rows:
            # Deferring the conflict or its candidate (memory API) both set it aside.
            deferred = row["resolution"] == "deferred" or row["candidate_status"] == "deferred"
            candidate_etag = (
                repository.candidate_etag(session, str(row["candidate_id"]))
                if row["candidate_id"]
                else None
            )
            items.append(
                PendingItem(
                    type=self.type,
                    id=str(row["id"]),
                    etag=sha256_json(
                        {
                            "id": str(row["id"]),
                            "resolution": row["resolution"],
                            "candidate_etag": candidate_etag,
                        }
                    ),
                    pending_since=instant(row["created_at"]),
                    actions=("restore",) if deferred else ("defer",),
                    detail_actions=("confirm", "reject", "supplement"),
                    requires_individual_approval=False,
                    deferred=deferred,
                    title=str(row["state_key"]),
                    detail={
                        "state_key": row["state_key"],
                        "resolution": row["resolution"],
                        "candidate_id": row["candidate_id"],
                        "candidate_status": row["candidate_status"],
                        "candidate_etag": candidate_etag,
                        "candidate_value": _payload(row["candidate_value_json"]),
                        "existing_value": _payload(row["formal_value_json"]),
                    },
                )
            )
        return items


class MissingInfoPromptSource:
    type = "missing_info_prompt"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        rows = session.execute(
            text(
                """
                SELECT id, state_key, reason, payload_json, status, created_at
                FROM personal_prompts
                WHERE owner_user_id = :owner AND prompt_kind = 'missing'
                  AND status IN ('pending', 'deferred')
                """
            ),
            {"owner": owner},
        ).mappings()
        items = []
        for row in rows:
            deferred = str(row["status"]) == "deferred"
            payload = _payload(row["payload_json"])
            items.append(
                PendingItem(
                    type=self.type,
                    id=str(row["id"]),
                    etag=sha256_json({"id": str(row["id"]), "status": row["status"]}),
                    pending_since=instant(row["created_at"]),
                    actions=("reject", "restore" if deferred else "defer"),
                    detail_actions=("supplement",),
                    requires_individual_approval=False,
                    deferred=deferred,
                    title=str(payload.get("query") or row["state_key"] or ""),
                    summary=str(row["reason"]),
                    detail={"state_key": row["state_key"], "payload": payload},
                )
            )
        return items


class ExtractionSupplementSource:
    type = "extraction_supplement"

    def __init__(self, deferrals: DeferralStore) -> None:
        self._deferrals = deferrals

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        deferred_ids = self._deferrals.deferred_ids(session, owner, self.type)
        rows = session.execute(
            text(
                """
                SELECT r.id, r.status, r.unrecognized_count, r.failure_reason,
                       r.created_at, r.updated_at, s.body AS source_text
                FROM conversation_extraction_runs r
                JOIN conclusion_sources s
                  ON s.id = r.source_id AND s.owner_user_id = r.owner_user_id
                WHERE r.owner_user_id = :owner
                  AND (r.unrecognized_count > 0 OR r.status = 'failed')
                  AND r.supplement_resolved_at IS NULL
                """
            ),
            {"owner": owner},
        ).mappings()
        items = []
        for row in rows:
            deferred = str(row["id"]) in deferred_ids
            items.append(
                PendingItem(
                    type=self.type,
                    id=str(row["id"]),
                    etag=sha256_json(
                        {
                            "id": str(row["id"]),
                            "updated_at": str(row["updated_at"]),
                            "deferred": deferred,
                        }
                    ),
                    pending_since=instant(row["created_at"]),
                    actions=("restore",) if deferred else ("defer",),
                    detail_actions=("supplement",),
                    requires_individual_approval=False,
                    deferred=deferred,
                    title=str(row["source_text"])[:80],
                    summary=str(row["failure_reason"]) if row["failure_reason"] else None,
                    detail={
                        "status": row["status"],
                        "unrecognized_count": row["unrecognized_count"],
                    },
                )
            )
        return items


class MemoryCandidateSource:
    type = "memory_candidate"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        # A candidate behind a personal conflict is decided through that
        # conflict, so it is listed (and counted) once, as the conflict.
        rows = session.execute(
            text(
                """
                SELECT mc.id, mc.candidate_type, mc.state_key, mc.status,
                       mc.current_version_id, mc.rationale, mc.created_at, mcv.value_json
                FROM memory_candidates mc
                JOIN memory_candidate_versions mcv ON mcv.id = mc.current_version_id
                WHERE mc.status IN ('pending_confirmation', 'edited', 'deferred')
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_conflicts c WHERE c.candidate_id = mc.id
                  )
                """
            )
        ).mappings()
        items = []
        for row in rows:
            value = _payload(row["value_json"])
            deferred = str(row["status"]) == "deferred"
            items.append(
                PendingItem(
                    type=self.type,
                    kind=str(row["candidate_type"]),
                    id=str(row["id"]),
                    etag=sha256_json(
                        {
                            "id": str(row["id"]),
                            "current_version_id": str(row["current_version_id"]),
                            "status": str(row["status"]),
                        }
                    ),
                    pending_since=instant(row["created_at"]),
                    # A deferred candidate must be restored before it is decided.
                    actions=("restore",) if deferred else ("approve", "reject", "defer"),
                    requires_individual_approval=str(row["candidate_type"]) == "inferred",
                    deferred=deferred,
                    title=str(row["state_key"]),
                    summary=str(value.get("text") or "") or None,
                    detail={"value": value, "rationale": row["rationale"]},
                )
            )
        return items


class ClassificationSource:
    """未分类资料 and 待审核的分类调整 share one type (filter 7)."""

    type = "classification"

    def items(self, session: Session, owner: str) -> list[PendingItem]:
        return self._unclassified(session, owner) + self._proposals(session, owner)

    def _unclassified(self, session: Session, owner: str) -> list[PendingItem]:
        active = {
            str(domain_id)
            for domain_id in session.execute(
                text("SELECT id FROM domain_catalog WHERE status = 'active'")
            ).scalars()
        }
        rows = session.execute(
            text(
                f"""
                SELECT ko.id, ko.title, ko.primary_domain_id, ko.created_at, ko.updated_at,
                       (SELECT substr(c.text, 1, 500) FROM chunks c
                        WHERE c.source_id = ko.id ORDER BY c.chunk_no LIMIT 1) AS excerpt,
                       EXISTS (
                         SELECT 1 FROM unclassified_deferrals ud
                         WHERE ud.owner_user_id = ko.owner_user_id
                           AND ud.knowledge_object_id = ko.id
                       ) AS deferred
                FROM knowledge_objects ko
                WHERE ko.owner_user_id = :owner
                  AND ko.lifecycle_status NOT IN ('privacy_erased', 'soft_deleted')
                  AND {UNCLASSIFIED_SQL}
                """
            ),
            {"owner": owner},
        ).mappings()
        items = []
        for row in rows:
            deferred = bool(row["deferred"])
            suggestion = suggested_primary_domain(
                str(row["primary_domain_id"]), str(row["title"]), str(row["excerpt"] or ""), active
            )
            actions: tuple[str, ...] = ("approve",) if suggestion else ()
            actions += ("restore",) if deferred else ("defer",)
            items.append(
                PendingItem(
                    type=self.type,
                    kind="unclassified",
                    id=str(row["id"]),
                    etag=sha256_json(
                        {
                            "id": str(row["id"]),
                            "updated_at": str(row["updated_at"]),
                            "primary_domain_id": row["primary_domain_id"],
                            "suggested_primary_domain_id": suggestion,
                            "deferred": deferred,
                        }
                    ),
                    pending_since=instant(row["created_at"]),
                    actions=actions,
                    detail_actions=() if suggestion else ("choose_domain",),
                    requires_individual_approval=False,
                    deferred=deferred,
                    title=str(row["title"]),
                    summary=str(row["excerpt"] or "")[:200] or None,
                    detail={
                        "primary_domain_id": row["primary_domain_id"],
                        "suggested_primary_domain_id": suggestion,
                    },
                )
            )
        return items

    def _proposals(self, session: Session, owner: str) -> list[PendingItem]:
        repository = TaxonomyRepository()
        proposals = [
            proposal
            for status in ("pending", "deferred")
            for proposal in repository.list_proposals(
                session, user_id=owner, status=status, limit=-1
            )
        ]
        items = []
        for proposal in proposals:
            deferred = proposal["status"] == "deferred"
            items.append(
                PendingItem(
                    type=self.type,
                    kind="taxonomy_proposal",
                    id=str(proposal["id"]),
                    etag=str(proposal["etag"]),
                    pending_since=instant(proposal["created_at"]),
                    actions=("approve", "reject", "restore" if deferred else "defer"),
                    requires_individual_approval=False,
                    deferred=deferred,
                    title=str(proposal["proposal_type"]),
                    summary=str(proposal["payload"].get("reason") or "") or None,
                    detail={
                        "proposal_type": proposal["proposal_type"],
                        "target_id": proposal["target_id"],
                    },
                )
            )
        return items


def _classification_action(action: str) -> ActionHandler:
    def handler(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        if action == "approve":
            return _approve_classification(session, owner, item)
        if item.kind == "unclassified":
            knowledge = ClassificationRepository()
            move = knowledge.defer_unclassified
            if action == "restore":
                move = knowledge.restore_unclassified
            return {"status": move(session, user_id=owner, knowledge_id=item.id)["status"]}
        taxonomy = TaxonomyRepository()
        if action == "reject":
            result = taxonomy.reject_proposal(
                session, user_id=owner, proposal_id=item.id, expected_etag=item.etag, reason=""
            )
        else:
            transition = (
                taxonomy.defer_proposal if action == "defer" else taxonomy.restore_proposal
            )
            result = transition(
                session, user_id=owner, proposal_id=item.id, expected_etag=item.etag
            )
        return {"status": result["status"]}

    return handler


def _approve_classification(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    if item.kind == "unclassified":
        domain_id = str(item.detail["suggested_primary_domain_id"])
        ClassificationRepository().assign(
            session,
            knowledge_id=item.id,
            user_id=owner,
            primary_domain_id=domain_id,
            node_ids=[],
            request_id=None,
        )
        return {"status": "classified", "primary_domain_id": domain_id}
    result = TaxonomyRepository().approve_proposal(
        session, user_id=owner, proposal_id=item.id, expected_etag=item.etag, request_id=None
    )
    return {"status": result["status"]}


def _decide_prompt(decision: str) -> ActionHandler:
    def handler(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        result = PersonalUpdateService().decide_missing_prompt(session, item.id, decision)
        return {"status": result["status"]}

    return handler


def _restore_prompt(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    return {"status": PersonalUpdateService().restore_missing_prompt(session, item.id)["status"]}


def _defer_conflict(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    return {
        "status": PersonalUpdateService().defer_conflict(session, item.id, owner_user_id=owner)[
            "status"
        ]
    }


def _restore_conflict(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    candidate_id = item.detail.get("candidate_id")
    if item.detail.get("candidate_status") == "deferred" and candidate_id:
        _restore_candidate_by_id(session, str(candidate_id), f"conflict:{item.id}:{item.etag}")
    if item.detail.get("resolution") == "deferred":
        PersonalUpdateService().restore_conflict(session, item.id, owner_user_id=owner)
    return {"status": "pending"}


def _restore_candidate_by_id(session: Session, candidate_id: str, key: str) -> None:
    repository = MemoryRepository()
    repository.restore_candidate(
        session,
        candidate_id=candidate_id,
        reason="restored from 待处理",
        operation_key=f"domain:candidate:restore:pending:{key}",
    )
    repository.request_confirmation(session, candidate_id=candidate_id)


def _defer_candidate(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    result = MemoryRepository().defer_candidate(
        session,
        candidate_id=item.id,
        reason="deferred from 待处理",
        operation_key=f"domain:candidate:defer:pending:{item.id}:{item.etag}",
    )
    return {"status": result.status}


def _decide_candidate(decision: str) -> ActionHandler:
    def handler(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        repository = MemoryRepository()
        request_id = repository.pending_request_id(
            session, item.id
        ) or repository.request_confirmation(session, candidate_id=item.id)
        decide = repository.confirm_request if decision == "approve" else repository.reject_request
        result = decide(
            session,
            request_id=request_id,
            operation_key=f"domain:candidate:{decision}:pending:{item.id}:{item.etag}",
        )
        return {"status": result.status, "formal_memory_id": result.formal_memory_id}

    return handler


def _restore_candidate(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    _restore_candidate_by_id(session, item.id, f"{item.id}:{item.etag}")
    return {"status": "pending"}


def _decide_relation(decision: str) -> ActionHandler:
    def handler(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        result = ConclusionRepository().decide_relation(
            session, owner, item.id, decision, f"pending:{decision}:{item.id}:{item.etag}"
        )
        return {"status": result["status"]}

    return handler


def _restore_relation(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    return {"status": ConclusionRepository().restore_relation(session, owner, item.id)["status"]}


def _decide_conclusion(decision: str) -> ActionHandler:
    def handler(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
        key = f"pending:{decision}:{item.id}:{item.etag}"
        result = ConclusionRepository().decide_draft(
            session, owner, item.id, item.etag, decision, key
        )
        return {"status": result["status"]}

    return handler


def _restore_conclusion(session: Session, owner: str, item: PendingItem) -> dict[str, Any]:
    return {"status": ConclusionRepository().restore_draft(session, owner, item.id)["status"]}


def build_inbox() -> PendingInbox:
    deferrals = DeferralStore()
    inbox = PendingInbox(
        [
            ConclusionDraftSource(),
            RelationSuggestionSource(),
            PersonalConflictSource(),
            MissingInfoPromptSource(),
            ExtractionSupplementSource(deferrals),
            MemoryCandidateSource(),
            ClassificationSource(),
        ]
    )
    # Approving a draft or relation writes formal knowledge, which needs the
    # app's object store: the API registers those two handlers on its inbox.
    # Conflicts and extraction supplements are decided in their detail view.
    inbox.register_action("conclusion_draft", "reject", _decide_conclusion("rejected"))
    inbox.register_action("conclusion_draft", "defer", _decide_conclusion("deferred"))
    inbox.register_action("conclusion_draft", "restore", _restore_conclusion)
    inbox.register_action("relation_suggestion", "reject", _decide_relation("rejected"))
    inbox.register_action("relation_suggestion", "defer", _decide_relation("deferred"))
    inbox.register_action("relation_suggestion", "restore", _restore_relation)
    inbox.register_action("personal_conflict", "defer", _defer_conflict)
    inbox.register_action("personal_conflict", "restore", _restore_conflict)
    inbox.register_action("missing_info_prompt", "reject", _decide_prompt("skip"))
    inbox.register_action("missing_info_prompt", "defer", _decide_prompt("defer"))
    inbox.register_action("missing_info_prompt", "restore", _restore_prompt)
    inbox.register_action("extraction_supplement", "defer", deferrals.defer)
    inbox.register_action("extraction_supplement", "restore", deferrals.restore)
    inbox.register_action("memory_candidate", "approve", _decide_candidate("approve"))
    inbox.register_action("memory_candidate", "reject", _decide_candidate("reject"))
    inbox.register_action("memory_candidate", "defer", _defer_candidate)
    inbox.register_action("memory_candidate", "restore", _restore_candidate)
    for action in ("approve", "reject", "defer", "restore"):
        inbox.register_action("classification", action, _classification_action(action))
    return inbox
