# ruff: noqa: E501
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json


class ConclusionRepository:
    def _write(
        self,
        session: Session,
        owner: str,
        key: str,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        digest = sha256_json(dict(payload))
        existing = (
            session.execute(
                text(
                    "SELECT request_hash,result_json FROM conclusion_operations WHERE owner_user_id=:o AND operation_key=:k"
                ),
                {"o": owner, "k": key},
            )
            .mappings()
            .first()
        )
        if existing:
            if str(existing["request_hash"]) != digest:
                raise ValueError("operation key already used with different payload")
            return cast(dict[str, Any], json.loads(str(existing["result_json"])))
        session.execute(
            text(
                "INSERT INTO conclusion_operations(owner_user_id,operation_key,request_hash,result_json) VALUES (:o,:k,:h,:r)"
            ),
            {"o": owner, "k": key, "h": digest, "r": json_text(dict(result))},
        )
        return dict(result)

    def create_source(self, session: Session, owner: str, body: str, key: str) -> dict[str, Any]:
        return self.persist_source(session, owner, body, key)

    def _replay(self, session: Session, owner: str, key: str) -> dict[str, Any]:
        row = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one()
        return cast(dict[str, Any], json.loads(str(row)))

    def persist_source(
        self,
        session: Session,
        owner: str,
        body: str,
        key: str,
        history_id: str | None = None,
    ) -> dict[str, Any]:
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing is not None:
            return cast(dict[str, Any], json.loads(str(existing)))
        source_id = new_id()
        session.execute(
            text(
                "INSERT INTO conclusion_sources(id,owner_user_id,body,history_id) "
                "VALUES (:id,:o,:b,:h)"
            ),
            {"id": source_id, "o": owner, "b": body, "h": history_id},
        )
        result = {"id": source_id, "text": body}
        return self._write(session, owner, key, {"kind": "source", "body": body}, result)

    def create_draft(
        self, session: Session, owner: str, source_id: str, payload: dict[str, Any], key: str
    ) -> dict[str, Any]:
        source = session.execute(
            text("SELECT id FROM conclusion_sources WHERE id=:id AND owner_user_id=:o"),
            {"id": source_id, "o": owner},
        ).first()
        if source is None:
            raise ValueError("source not found")
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing:
            return cast(dict[str, Any], json.loads(str(existing)))
        entry_id, version = new_id(), 1
        row = {**payload, "status": "draft", "version": version}
        session.execute(
            text(
                "INSERT INTO conclusion_entries(id,owner_user_id,source_id,current_version,status) VALUES (:id,:o,:s,:v,'draft')"
            ),
            {"id": entry_id, "o": owner, "s": source_id, "v": version},
        )
        session.execute(
            text(
                "INSERT INTO conclusion_versions(entry_id,version,payload_json) VALUES (:id,:v,:p)"
            ),
            {"id": entry_id, "v": version, "p": json_text(row)},
        )
        result = {
            "id": entry_id,
            "etag": sha256_json({"id": entry_id, "version": version, "status": "draft"}),
            **row,
        }
        return self._write(session, owner, key, payload, result)

    def get(self, session: Session, owner: str, entry_id: str) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    "SELECT e.*,v.payload_json,v.version FROM conclusion_entries e JOIN conclusion_versions v ON v.entry_id=e.id AND v.version=e.current_version WHERE e.id=:id AND e.owner_user_id=:o"
                ),
                {"id": entry_id, "o": owner},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        source = (
            session.execute(
                text("SELECT id,body FROM conclusion_sources WHERE id=:id"),
                {"id": row["source_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "id": entry_id,
            "status": row["status"],
            "version": row["version"],
            "approved_version": row["approved_version"],
            "etag": sha256_json(
                {"id": entry_id, "version": row["version"], "status": row["status"]}
            ),
            **payload,
            "source": {"id": source["id"], "text": source["body"]},
        }

    def list_drafts(self, session: Session, owner: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT id FROM conclusion_entries
                WHERE owner_user_id = :owner AND status = 'draft'
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT :limit
                """
            ),
            {"owner": owner, "limit": max(1, min(limit, 500))},
        ).scalars()
        return [item for entry_id in rows if (item := self.get(session, owner, str(entry_id))) is not None]

    def update_draft(
        self,
        session: Session,
        owner: str,
        entry_id: str,
        etag: str,
        changes: Mapping[str, Any],
        key: str,
    ) -> dict[str, Any]:
        item = self.get(session, owner, entry_id)
        if item is None or item["status"] != "draft":
            raise ValueError("draft conclusion not found")
        if etag not in {"*", item["etag"]}:
            raise ValueError("conclusion changed after it was read")
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations "
                "WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing is not None:
            return cast(dict[str, Any], json.loads(str(existing)))
        payload = {
            key: value for key, value in {**item, **dict(changes)}.items()
            if key not in {"id", "status", "version", "approved_version", "etag", "source"}
        }
        version = int(item["version"]) + 1
        session.execute(
            text(
                "INSERT INTO conclusion_versions(entry_id,version,payload_json) "
                "VALUES (:id,:v,:p)"
            ),
            {"id": entry_id, "v": version, "p": json_text({**payload, "status": "draft", "version": version})},
        )
        session.execute(
            text(
                "UPDATE conclusion_entries SET current_version=:v WHERE id=:id AND owner_user_id=:o"
            ),
            {"id": entry_id, "o": owner, "v": version},
        )
        result = {
            "id": entry_id,
            "status": "draft",
            "version": version,
            "approved_version": None,
            "etag": sha256_json({"id": entry_id, "version": version, "status": "draft"}),
            **payload,
            "source": item["source"],
        }
        return self._write(session, owner, key, changes, result)

    def approve(
        self, session: Session, owner: str, entry_id: str, etag: str, key: str
    ) -> dict[str, Any]:
        item = self.get(session, owner, entry_id)
        if item is None:
            raise ValueError("conclusion not found")
        if etag not in {"*", item["etag"]}:
            raise ValueError("conclusion changed after it was read")
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing:
            return cast(dict[str, Any], json.loads(str(existing)))
        session.execute(
            text(
                "UPDATE conclusion_entries SET status='formal',approved_version=current_version WHERE id=:id AND owner_user_id=:o"
            ),
            {"id": entry_id, "o": owner},
        )
        session.execute(
            text(
                "UPDATE conclusion_versions SET approved_at=CURRENT_TIMESTAMP WHERE entry_id=:id AND version=:v"
            ),
            {"id": entry_id, "v": item["version"]},
        )
        result = {**item, "status": "formal", "approved_version": item["version"]}
        return self._write(session, owner, key, {"entry_id": entry_id, "etag": etag}, result)

    def context(self, session: Session, owner: str, query: str) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                "SELECT e.id,e.current_version,e.approved_version,v.payload_json FROM conclusion_entries e JOIN conclusion_versions v ON v.entry_id=e.id AND v.version=e.approved_version WHERE e.owner_user_id=:o AND e.status='formal' AND v.payload_json LIKE :q"
            ),
            {"o": owner, "q": f"%{query}%"},
        ).mappings()
        out = []
        for row in rows:
            p = json.loads(str(row["payload_json"]))
            premises = p.get("premises", [])
            text_value = p.get("claim", "")
            if any(not x.get("confirmed", False) for x in premises):
                text_value = (
                    "如果"
                    + "、".join(
                        str(x.get("text", "")) for x in premises if not x.get("confirmed", False)
                    )
                    + "，则"
                    + text_value
                )
            out.append(
                {
                    "id": row["id"],
                    "text": text_value,
                    "premises": premises,
                    "domain_id": p.get("domain_id"),
                    "source_excerpt": p.get("excerpt"),
                }
            )
        return out

    def attach_knowledge(
        self, session: Session, owner: str, entry_id: str, knowledge_id: str
    ) -> None:
        session.execute(
            text("UPDATE conclusion_entries SET knowledge_id=:k WHERE id=:id AND owner_user_id=:o"),
            {"k": knowledge_id, "id": entry_id, "o": owner},
        )
