"""Durable, owner-scoped conversation and answer-history persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id


class ConversationRepository:
    def create(
        self, session: Session, *, owner_user_id: str, title: str | None = None
    ) -> dict[str, Any]:
        conversation_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO answer_conversations (id, owner_user_id, title)
                VALUES (:id, :owner, :title)
                """
            ),
            {"id": conversation_id, "owner": owner_user_id, "title": title},
        )
        return {
            "id": conversation_id,
            "owner_user_id": owner_user_id,
            "title": title,
            "turn_count": 0,
        }

    def get_owned(
        self, session: Session, *, conversation_id: str, owner_user_id: str
    ) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, title, created_at, updated_at, archived_at
                FROM answer_conversations
                WHERE id=:id AND owner_user_id=:owner
                """
                ),
                {"id": conversation_id, "owner": owner_user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        count = session.execute(
            text(
                "SELECT count(*) FROM answer_history "
                "WHERE conversation_id=:id AND owner_user_id=:owner"
            ),
            {"id": conversation_id, "owner": owner_user_id},
        ).scalar_one()
        return {**dict(row), "turn_count": int(count)}

    def list_owned(
        self, session: Session, *, owner_user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at, c.archived_at,
                       count(h.id) AS turn_count
                FROM answer_conversations c
                LEFT JOIN answer_history h ON h.conversation_id=c.id
                WHERE c.owner_user_id=:owner
                GROUP BY c.id
                ORDER BY c.updated_at DESC, c.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"owner": owner_user_id, "limit": limit, "offset": offset},
        ).mappings()
        return [dict(row) for row in rows]

    def context(
        self, session: Session, *, conversation_id: str, owner_user_id: str, limit: int = 6
    ) -> list[dict[str, str]]:
        rows = session.execute(
            text(
                """
                SELECT query, response_json
                FROM answer_history
                WHERE conversation_id=:conversation_id AND owner_user_id=:owner
                ORDER BY turn_index DESC
                LIMIT :limit
                """
            ),
            {
                "conversation_id": conversation_id,
                "owner": owner_user_id,
                "limit": max(1, min(limit, 12)),
            },
        ).mappings()
        context: list[dict[str, str]] = []
        for row in reversed(list(rows)):
            try:
                payload = json.loads(str(row["response_json"]))
                answer = str(payload.get("answer", ""))[:4000] if isinstance(payload, dict) else ""
            except (TypeError, ValueError, json.JSONDecodeError):
                answer = ""
            context.append({"query": str(row["query"])[:4000], "answer": answer})
        return context

    def append(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        query: str,
        response: dict[str, Any],
    ) -> str:
        turn_index = int(
            session.execute(
                text(
                    "SELECT coalesce(max(turn_index), 0) + 1 FROM answer_history "
                    "WHERE conversation_id=:conversation_id AND owner_user_id=:owner"
                ),
                {"conversation_id": conversation_id, "owner": owner_user_id},
            ).scalar_one()
        )
        history_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO answer_history
                (id, conversation_id, owner_user_id, turn_index, query, response_json,
                 route, stop_reason)
                VALUES (:id, :conversation_id, :owner, :turn_index, :query, :response,
                        :route, :stop_reason)
                """
            ),
            {
                "id": history_id,
                "conversation_id": conversation_id,
                "owner": owner_user_id,
                "turn_index": turn_index,
                "query": query,
                "response": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                "route": str(response.get("route", {}).get("route", "unknown")),
                "stop_reason": str(response.get("stop_reason", "unknown")),
            },
        )
        session.execute(
            text(
                "UPDATE answer_conversations SET updated_at=:now "
                "WHERE id=:id AND owner_user_id=:owner"
            ),
            {"now": datetime.now(UTC), "id": conversation_id, "owner": owner_user_id},
        )
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'conversation.persisted', 'answer_history', :aggregate_id,
                  :payload_json, 'pending'
                )
                """
            ),
            {
                "id": f"conversation:{history_id}",
                "aggregate_id": history_id,
                "payload_json": json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "history_id": history_id,
                        "owner_user_id": owner_user_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        return history_id

    def list_history(
        self,
        session: Session,
        *,
        owner_user_id: str,
        conversation_id: str | None,
        favorite: bool | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        conditions = ["owner_user_id=:owner"]
        params: dict[str, Any] = {"owner": owner_user_id, "limit": limit, "offset": offset}
        if conversation_id:
            conditions.append("conversation_id=:conversation_id")
            params["conversation_id"] = conversation_id
        if favorite is not None:
            conditions.append("is_favorite=:favorite")
            params["favorite"] = favorite
        rows = session.execute(
            text(
                "SELECT id, conversation_id, turn_index, query, response_json, route, stop_reason, "
                "is_favorite, created_at FROM answer_history WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["response"] = json.loads(str(item.pop("response_json")))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["response"] = {}
            item["is_favorite"] = bool(item["is_favorite"])
            result.append(item)
        return result

    def set_favorite(
        self, session: Session, *, history_id: str, owner_user_id: str, favorite: bool
    ) -> bool:
        result = session.execute(
            text(
                "UPDATE answer_history SET is_favorite=:favorite "
                "WHERE id=:id AND owner_user_id=:owner"
            ),
            {"favorite": favorite, "id": history_id, "owner": owner_user_id},
        )
        return result.rowcount > 0
