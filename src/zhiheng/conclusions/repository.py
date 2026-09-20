# ruff: noqa: E501
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.conclusions.applicability import ConclusionApplicabilityService
from zhiheng.conclusions.classification import normalize_classification
from zhiheng.conclusions.relations import relation_explanation, relation_kind
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
        classification = normalize_classification(
            payload.get("classification"),
            fallback_domain_id=str(payload.get("domain_id", "education_learning")),
            title=str(payload.get("title", "")),
            claim=str(payload.get("claim", "")),
        )
        payload = {
            **payload,
            "domain_id": classification["primary_domain_id"],
            "record_type": classification["record_type"],
            "classification": classification,
        }
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
        created = self._write(session, owner, key, payload, result)
        self.suggest_relations(session, owner, entry_id)
        return created

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
        ConclusionApplicabilityService().sweep(session)
        payload = json.loads(str(row["payload_json"]))
        source = (
            session.execute(
                text("SELECT id,body FROM conclusion_sources WHERE id=:id"),
                {"id": row["source_id"]},
            )
            .mappings()
            .one()
        )
        result = {
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
        result["status"] = row["status"]
        result["applicability"] = ConclusionApplicabilityService().describe(session, entry_id)
        result["relations"] = self.list_relations(session, owner, entry_id)
        return result

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
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations "
                "WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing is not None:
            return cast(dict[str, Any], json.loads(str(existing)))
        if etag not in {"*", item["etag"]}:
            raise ValueError("conclusion changed after it was read")
        payload = {
            key: value for key, value in {**item, **dict(changes)}.items()
            if key not in {
                "id",
                "status",
                "version",
                "approved_version",
                "etag",
                "source",
                "relations",
                "applicability",
            }
        }
        if "classification" in changes:
            classification = normalize_classification(
                cast(dict[str, Any] | None, changes["classification"]),
                fallback_domain_id=str(payload.get("domain_id", "education_learning")),
                title=str(payload.get("title", "")),
                claim=str(payload.get("claim", "")),
            )
            payload["classification"] = classification
            payload["domain_id"] = classification["primary_domain_id"]
            payload["record_type"] = classification["record_type"]
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
        self.suggest_relations(session, owner, entry_id, key=f"conclusion-relations:{entry_id}:v{version}")
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
        ConclusionApplicabilityService().sweep(session)
        rows = session.execute(
            text(
                "SELECT e.id,e.current_version,e.approved_version,v.payload_json FROM conclusion_entries e JOIN conclusion_versions v ON v.entry_id=e.id AND v.version=e.approved_version WHERE e.owner_user_id=:o AND e.status='formal' AND NOT EXISTS (SELECT 1 FROM conclusion_applicability a WHERE a.entry_id=e.id AND a.version=e.approved_version AND a.state='suspended') AND v.payload_json LIKE :q"
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

    def suggest_relations(
        self, session: Session, owner: str, entry_id: str, key: str | None = None
    ) -> list[dict[str, Any]]:
        """Create explainable proposals against the owner's approved conclusions."""
        item = self.get(session, owner, entry_id)
        if item is None:
            raise ValueError("conclusion not found")
        operation_key = key or f"conclusion-relations:{entry_id}"
        existing_operation = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations "
                "WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": operation_key},
        ).scalar_one_or_none()
        if existing_operation is not None:
            stored = cast(dict[str, Any], json.loads(str(existing_operation)))
            return cast(list[dict[str, Any]], stored["items"])

        rows = session.execute(
            text(
                """
                SELECT e.id, e.source_id, v.payload_json
                FROM conclusion_entries e
                JOIN conclusion_versions v
                  ON v.entry_id=e.id AND v.version=e.approved_version
                WHERE e.owner_user_id=:o AND e.status='formal' AND e.id != :id
                ORDER BY e.created_at ASC, e.id ASC
                """
            ),
            {"o": owner, "id": entry_id},
        ).mappings()
        proposals: list[dict[str, Any]] = []
        for row in rows:
            old_payload = cast(dict[str, Any], json.loads(str(row["payload_json"])))
            kind = relation_kind(item, old_payload)
            if kind is None:
                continue
            duplicate = session.execute(
                text(
                    "SELECT id FROM conclusion_relations "
                    "WHERE owner_user_id=:o AND left_id=:left AND right_id=:right "
                    "AND kind=:kind AND left_version=:lv AND right_version=:rv "
                    "AND status IN ('proposed','approved','deferred')"
                ),
                {
                    "o": owner,
                    "left": entry_id,
                    "right": row["id"],
                    "kind": kind,
                    "lv": item["version"],
                    "rv": old_payload.get("version", 1),
                },
            ).scalar_one_or_none()
            if duplicate is not None:
                continue
            relation_id = new_id()
            session.execute(
                text(
                    "INSERT INTO conclusion_relations "
                    "(id,owner_user_id,left_id,right_id,kind,status,left_version,right_version) "
                    "VALUES (:id,:o,:left,:right,:kind,'proposed',:lv,:rv)"
                ),
                {
                    "id": relation_id,
                    "o": owner,
                    "left": entry_id,
                    "right": row["id"],
                    "kind": kind,
                    "lv": item["version"],
                    "rv": old_payload.get("version", 1),
                },
            )
            session.execute(
                text(
                    "INSERT INTO conclusion_relation_events "
                    "(id,relation_id,owner_user_id,from_status,to_status,actor_user_id,"
                    "left_version,right_version,left_source_id,right_source_id) "
                    "VALUES (:id,:relation,:o,NULL,'proposed',:actor,:lv,:rv,:ls,:rs)"
                ),
                {
                    "id": new_id(),
                    "relation": relation_id,
                    "o": owner,
                    "actor": owner,
                    "lv": item["version"],
                    "rv": old_payload.get("version", 1),
                    "ls": item["source"]["id"],
                    "rs": row["source_id"],
                },
            )
            proposals.append(
                {
                    "id": relation_id,
                    "left_id": entry_id,
                    "right_id": row["id"],
                    "kind": kind,
                    "status": "proposed",
                    "left_version": item["version"],
                    "right_version": old_payload.get("version", 1),
                    "left_source_id": item["source"]["id"],
                    "right_source_id": row["source_id"],
                    "explanation": relation_explanation(kind),
                }
            )
        self._write(session, owner, operation_key, {"entry_id": entry_id}, {"items": proposals})
        return proposals

    def list_relations(
        self, session: Session, owner: str, entry_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        where_status = " AND r.status=:status" if status else ""
        rows = session.execute(
            text(
                """
                SELECT r.*, le.source_id AS left_source_id, re.source_id AS right_source_id,
                       lv.payload_json AS left_payload, rv.payload_json AS right_payload
                FROM conclusion_relations r
                JOIN conclusion_entries le ON le.id=r.left_id AND le.owner_user_id=r.owner_user_id
                JOIN conclusion_entries re ON re.id=r.right_id AND re.owner_user_id=r.owner_user_id
                JOIN conclusion_versions lv ON lv.entry_id=r.left_id AND lv.version=r.left_version
                JOIN conclusion_versions rv ON rv.entry_id=r.right_id AND rv.version=r.right_version
                WHERE r.owner_user_id=:o AND (r.left_id=:id OR r.right_id=:id)
                """ + where_status + " ORDER BY r.id DESC"
            ),
            {"o": owner, "id": entry_id, **({"status": status} if status else {})},
        ).mappings()
        out = []
        for row in rows:
            left_payload = cast(dict[str, Any], json.loads(str(row["left_payload"])))
            right_payload = cast(dict[str, Any], json.loads(str(row["right_payload"])))
            out.append(
                {
                    "id": row["id"],
                    "left_id": row["left_id"],
                    "right_id": row["right_id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "left_version": row["left_version"],
                    "right_version": row["right_version"],
                    "left_source_id": row["left_source_id"],
                    "right_source_id": row["right_source_id"],
                    "left_claim": left_payload.get("claim"),
                    "right_claim": right_payload.get("claim"),
                    "explanation": relation_explanation(str(row["kind"])),
                }
            )
        return out

    def decide_relation(
        self,
        session: Session,
        owner: str,
        relation_id: str,
        decision: str,
        key: str,
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected", "deferred"}:
            raise ValueError("invalid relation decision")
        row = session.execute(
            text(
                "SELECT r.*, le.source_id AS left_source_id, re.source_id AS right_source_id "
                "FROM conclusion_relations r "
                "JOIN conclusion_entries le ON le.id=r.left_id "
                "JOIN conclusion_entries re ON re.id=r.right_id "
                "WHERE r.id=:id AND r.owner_user_id=:o"
            ),
            {"id": relation_id, "o": owner},
        ).mappings().first()
        if row is None:
            raise ValueError("relation not found")
        if row["status"] not in {"proposed", "deferred"}:
            raise ValueError("relation is no longer reviewable")
        existing = session.execute(
            text(
                "SELECT result_json FROM conclusion_operations "
                "WHERE owner_user_id=:o AND operation_key=:k"
            ),
            {"o": owner, "k": key},
        ).scalar_one_or_none()
        if existing is not None:
            return cast(dict[str, Any], json.loads(str(existing)))
        session.execute(
            text("UPDATE conclusion_relations SET status=:status WHERE id=:id AND owner_user_id=:o"),
            {"status": decision, "id": relation_id, "o": owner},
        )
        if decision == "approved":
            session.execute(
                text(
                    "UPDATE conclusion_entries SET status='formal',approved_version=current_version "
                    "WHERE id=:id AND owner_user_id=:o AND status='draft'"
                ),
                {"id": row["left_id"], "o": owner},
            )
            session.execute(
                text(
                    "UPDATE conclusion_versions SET approved_at=CURRENT_TIMESTAMP "
                    "WHERE entry_id=:id AND version=:v"
                ),
                {"id": row["left_id"], "v": row["left_version"]},
            )
            if row["kind"] == "revision":
                session.execute(
                    text(
                        "UPDATE conclusion_entries SET status='superseded' "
                        "WHERE id=:id AND owner_user_id=:o AND status='formal'"
                    ),
                    {"id": row["right_id"], "o": owner},
                )
        session.execute(
            text(
                "INSERT INTO conclusion_relation_events "
                "(id,relation_id,owner_user_id,from_status,to_status,actor_user_id,"
                "left_version,right_version,left_source_id,right_source_id) "
                "VALUES (:id,:relation,:o,:from_status,:to_status,:actor,:lv,:rv,:ls,:rs)"
            ),
            {
                "id": new_id(),
                "relation": relation_id,
                "o": owner,
                "from_status": row["status"],
                "to_status": decision,
                "actor": owner,
                "lv": row["left_version"],
                "rv": row["right_version"],
                "ls": row["left_source_id"],
                "rs": row["right_source_id"],
            },
        )
        result = {
            "id": relation_id,
            "left_id": row["left_id"],
            "right_id": row["right_id"],
            "kind": row["kind"],
            "status": decision,
            "left_version": row["left_version"],
            "right_version": row["right_version"],
        }
        return self._write(session, owner, key, {"relation_id": relation_id, "decision": decision}, result)
