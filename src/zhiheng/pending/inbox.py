"""Aggregate every 待处理事项 into one uniform shape (ADR 0011).

Each pending type has a *source* that reads its own tables and returns
``PendingItem`` values.  Actions that the inbox itself can execute are
registered per (type, action); a ticket that adds a new verb for a type
registers its handler here and adds the verb to the source's ``actions``.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session


class PendingType(StrEnum):
    CONCLUSION_DRAFT = "conclusion_draft"
    RELATION_SUGGESTION = "relation_suggestion"
    PERSONAL_CONFLICT = "personal_conflict"
    MISSING_INFO_PROMPT = "missing_info_prompt"
    EXTRACTION_SUPPLEMENT = "extraction_supplement"
    MEMORY_CANDIDATE = "memory_candidate"
    CLASSIFICATION = "classification"


# In the order the inbox lists types (filters 1-7).
PENDING_TYPES: tuple[str, ...] = tuple(PendingType)

DeferredFilter = Literal["exclude", "only", "include"]
SortOrder = Literal["oldest", "newest"]


class PendingItemNotFoundError(LookupError):
    pass


class UnknownPendingTypeError(ValueError):
    pass


def require_known_types(types: Iterable[str]) -> builtins.list[str]:
    """The one check for a pending type name, shared by the API and the inbox."""
    selected = builtins.list(types)
    unknown = [item_type for item_type in selected if item_type not in PENDING_TYPES]
    if unknown:
        raise UnknownPendingTypeError(f"unknown pending type {unknown[0]}")
    return selected


class StalePendingItemError(ValueError):
    pass


class ActionUnavailableError(ValueError):
    pass


class IndividualApprovalRequiredError(ValueError):
    """A batch approve named an item that must be approved on its own (ADR 0011)."""


@dataclass(frozen=True)
class BatchTarget:
    type: str
    id: str
    etag: str


@dataclass(frozen=True)
class PendingItem:
    type: str
    id: str
    etag: str
    # When the item became pending.  Deferring and restoring never move it.
    pending_since: datetime
    actions: tuple[str, ...]
    requires_individual_approval: bool
    deferred: bool
    title: str
    kind: str | None = None
    summary: str | None = None
    # Decisions that need the detail view (choose, clarify, supplement).
    detail_actions: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "kind": self.kind,
            "id": self.id,
            "etag": self.etag,
            "pending_since": self.pending_since.isoformat(),
            "actions": list(self.actions),
            "detail_actions": list(self.detail_actions),
            "requires_individual_approval": self.requires_individual_approval,
            "deferred": self.deferred,
            "title": self.title,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


class PendingSource(Protocol):
    type: str

    def items(self, session: Session, owner: str) -> list[PendingItem]: ...


ActionHandler = Callable[[Session, str, PendingItem], Mapping[str, Any]]


def to_utc(value: object) -> datetime:
    """Normalise the mixed timestamp storage formats to aware UTC datetimes."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class PendingInbox:
    def __init__(self, sources: Iterable[PendingSource]) -> None:
        self._sources = {source.type: source for source in sources}
        self._handlers: dict[tuple[str, str], ActionHandler] = {}

    def register_action(self, item_type: str, action: str, handler: ActionHandler) -> None:
        require_known_types([item_type])
        self._handlers[(item_type, action)] = handler

    def collect(
        self, session: Session, owner: str, types: Sequence[str] | None = None
    ) -> list[PendingItem]:
        selected = require_known_types(types) if types else builtins.list(PENDING_TYPES)
        return [
            _uniformly_deferred(item)
            for item_type in selected
            if item_type in self._sources
            for item in self._sources[item_type].items(session, owner)
        ]

    def list(
        self,
        session: Session,
        owner: str,
        *,
        types: Sequence[str] | None = None,
        deferred: DeferredFilter = "exclude",
        order: SortOrder = "oldest",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        items = [
            item
            for item in self.collect(session, owner, types)
            if deferred == "include" or item.deferred == (deferred == "only")
        ]
        items.sort(key=lambda item: (item.pending_since, item.type, item.id))
        if order == "newest":
            items.reverse()
        page = items[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(items) else None
        return {
            "items": [item.to_dict() for item in page],
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
        }

    def counts(self, session: Session, owner: str) -> dict[str, Any]:
        by_type = dict.fromkeys(PENDING_TYPES, 0)
        for item in self.collect(session, owner):
            if not item.deferred:
                by_type[item.type] += 1
        return {"total": sum(by_type.values()), "by_type": by_type}

    def get(self, session: Session, owner: str, item_type: str, item_id: str) -> PendingItem:
        for item in self.collect(session, owner, [item_type]):
            if item.id == item_id:
                return item
        raise PendingItemNotFoundError("pending item not found")

    def perform(
        self,
        session: Session,
        owner: str,
        item_type: str,
        item_id: str,
        action: str,
        if_match: str,
    ) -> dict[str, Any]:
        item = self.get(session, owner, item_type, item_id)
        if if_match != item.etag:
            raise StalePendingItemError("pending item changed after it was read")
        handler = self._handlers.get((item_type, action))
        if handler is None or action not in item.actions:
            raise ActionUnavailableError(f"{action} is not available for this item")
        result = dict(handler(session, owner, item))
        return {"type": item_type, "id": item_id, "action": action, **result}

    def perform_batch(
        self, session: Session, owner: str, action: str, targets: Sequence[BatchTarget]
    ) -> builtins.list[dict[str, Any]]:
        """Run one action item by item; a failed item rolls back only itself."""
        if action == "approve":
            self._refuse_individual_approvals(session, owner, targets)
        results = []
        for target in targets:
            savepoint = session.begin_nested()
            try:
                outcome = self.perform(
                    session, owner, target.type, target.id, action, target.etag
                )
            except (LookupError, ValueError) as exc:
                savepoint.rollback()
                results.append(_batch_result(target, "failed", _reason(exc), str(exc), None))
            else:
                savepoint.commit()
                results.append(_batch_result(target, "succeeded", None, None, outcome))
        return results

    def _refuse_individual_approvals(
        self, session: Session, owner: str, targets: Sequence[BatchTarget]
    ) -> None:
        types = sorted({target.type for target in targets})
        # A deferred item fails on its own as action_unavailable, like any other verb.
        individual = {
            (item.type, item.id)
            for item in self.collect(session, owner, types)
            if item.requires_individual_approval and not item.deferred
        }
        for target in targets:
            if (target.type, target.id) in individual:
                raise IndividualApprovalRequiredError(
                    f"{target.type} {target.id} must be approved on its own"
                )


def _uniformly_deferred(item: PendingItem) -> PendingItem:
    """延后 means the same for every type: a deferred item can only be restored (ADR 0011)."""
    if not item.deferred:
        return item
    return replace(item, actions=("restore",), detail_actions=())


def _reason(exc: Exception) -> str:
    """One reason code per failure, whatever status each type's own API would use."""
    if isinstance(exc, PendingItemNotFoundError):
        return "not_found"
    if isinstance(exc, StalePendingItemError):
        return "stale"
    if isinstance(exc, ActionUnavailableError):
        return "action_unavailable"
    return "conflict"


def _batch_result(
    target: BatchTarget,
    status: str,
    reason: str | None,
    message: str | None,
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "type": target.type,
        "id": target.id,
        "status": status,
        "reason": reason,
        "message": message,
        "result": dict(result) if result is not None else None,
    }
