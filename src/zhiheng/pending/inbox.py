"""Aggregate every 待处理事项 into one uniform shape (ADR 0011).

Each pending type has a *source* that reads its own tables and returns
``PendingItem`` values.  Actions that the inbox itself can execute are
registered per (type, action); a ticket that adds a new verb for a type
registers its handler here and adds the verb to the source's ``actions``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

PENDING_TYPES: tuple[str, ...] = (
    "conclusion_draft",
    "relation_suggestion",
    "personal_conflict",
    "missing_info_prompt",
    "extraction_supplement",
    "memory_candidate",
    "classification",
)

DeferredFilter = Literal["exclude", "only", "include"]
SortOrder = Literal["oldest", "newest"]


class PendingItemNotFoundError(LookupError):
    pass


class StalePendingItemError(ValueError):
    pass


class ActionUnavailableError(ValueError):
    pass


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


def instant(value: object) -> datetime:
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
        if item_type not in self._sources:
            raise ValueError(f"unknown pending type {item_type}")
        self._handlers[(item_type, action)] = handler

    def collect(
        self, session: Session, owner: str, types: Sequence[str] | None = None
    ) -> list[PendingItem]:
        selected = list(types) if types else [t for t in PENDING_TYPES if t in self._sources]
        unknown = [item_type for item_type in selected if item_type not in self._sources]
        if unknown:
            raise ValueError(f"unknown pending type {unknown[0]}")
        return [
            item
            for item_type in selected
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
