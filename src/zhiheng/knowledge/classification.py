from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id


@dataclass(frozen=True)
class ClassificationNode:
    id: str
    domain_id: str
    parent_id: str | None
    level: int
    name: str
    description: str
    path: str
    sort_order: int
    status: str


class ClassificationRepository:
    def list_domains(self, session: Session, *, user_id: str) -> list[dict[str, Any]]:
        domains = (
            session.execute(
                text(
                    """
                SELECT id, name, description, sort_order, schema_version, status
                FROM domain_catalog WHERE status = 'active' ORDER BY sort_order, id
                """
                )
            )
            .mappings()
            .all()
        )
        nodes = (
            session.execute(
                text(
                    """
                SELECT id, domain_id, parent_id, level, name, description, path,
                       sort_order, status
                FROM classification_nodes
                WHERE owner_user_id = :user_id
                ORDER BY domain_id, level, sort_order, name, id
                """
                ),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
        by_domain: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            by_domain.setdefault(str(node["domain_id"]), []).append(dict(node))
        return [
            {**dict(domain), "nodes": by_domain.get(str(domain["id"]), [])} for domain in domains
        ]

    def get_node(self, session: Session, *, node_id: str, user_id: str) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    """
                    SELECT id, domain_id, parent_id, level, name, description, path,
                           sort_order, status, owner_user_id
                    FROM classification_nodes
                    WHERE id = :id AND owner_user_id = :user_id
                    """
                ),
                {"id": node_id, "user_id": user_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def create_node(
        self,
        session: Session,
        *,
        user_id: str,
        domain_id: str,
        parent_id: str | None,
        name: str,
        description: str,
        sort_order: int,
    ) -> dict[str, Any]:
        domain = session.execute(
            text("SELECT id FROM domain_catalog WHERE id = :id AND status = 'active'"),
            {"id": domain_id},
        ).scalar_one_or_none()
        if domain is None:
            raise ValueError("unknown or inactive domain")
        level = 2
        path = name
        if parent_id:
            parent = self.get_node(session, node_id=parent_id, user_id=user_id)
            if parent is None:
                raise ValueError("parent classification not found")
            if int(parent["level"]) >= 3:
                raise ValueError("classification depth cannot exceed three levels")
            if str(parent["domain_id"]) != domain_id:
                raise ValueError("parent must belong to the same domain")
            level = int(parent["level"]) + 1
            path = f"{parent['path']} / {name}"
        node_id = new_id()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO classification_nodes
                      (id, owner_user_id, domain_id, parent_id, level, name, description,
                       path, sort_order, status)
                    VALUES
                      (:id, :owner_user_id, :domain_id, :parent_id, :level, :name, :description,
                       :path, :sort_order, 'active')
                    """
                ),
                {
                    "id": node_id,
                    "owner_user_id": user_id,
                    "domain_id": domain_id,
                    "parent_id": parent_id,
                    "level": level,
                    "name": name.strip(),
                    "description": description,
                    "path": path,
                    "sort_order": sort_order,
                },
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("classification name already exists under parent") from exc
            raise
        return dict(self.get_node(session, node_id=node_id, user_id=user_id) or {})

    def update_node(
        self,
        session: Session,
        *,
        node_id: str,
        user_id: str,
        name: str | None,
        description: str | None,
        sort_order: int | None,
        status: str | None,
    ) -> dict[str, Any]:
        current = self.get_node(session, node_id=node_id, user_id=user_id)
        if current is None:
            raise ValueError("classification not found")
        next_name = name.strip() if name is not None else str(current["name"])
        if not next_name:
            raise ValueError("classification name cannot be empty")
        next_path = next_name
        if current["parent_id"]:
            parent = self.get_node(session, node_id=str(current["parent_id"]), user_id=user_id)
            if parent is None:
                raise ValueError("parent classification not found")
            next_path = f"{parent['path']} / {next_name}"
        session.execute(
            text(
                """
                UPDATE classification_nodes
                SET name = :name, description = :description, sort_order = :sort_order,
                    status = :status, path = :path, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :user_id
                """
            ),
            {
                "id": node_id,
                "user_id": user_id,
                "name": next_name,
                "description": description if description is not None else current["description"],
                "sort_order": sort_order if sort_order is not None else current["sort_order"],
                "status": status if status is not None else current["status"],
                "path": next_path,
            },
        )
        return dict(self.get_node(session, node_id=node_id, user_id=user_id) or {})

    def delete_node(self, session: Session, *, node_id: str, user_id: str) -> None:
        node = self.get_node(session, node_id=node_id, user_id=user_id)
        if node is None:
            raise ValueError("classification not found")
        child_count = session.execute(
            text(
                """
                SELECT count(*) FROM classification_nodes
                WHERE parent_id = :id AND owner_user_id = :user_id
                """
            ),
            {"id": node_id, "user_id": user_id},
        ).scalar_one()
        ref_count = session.execute(
            text(
                """
                SELECT count(*) FROM knowledge_classifications kc
                JOIN knowledge_objects ko ON ko.id = kc.knowledge_object_id
                WHERE kc.classification_node_id = :id AND ko.owner_user_id = :user_id
                  AND kc.confirmation_status = 'confirmed'
                """
            ),
            {"id": node_id, "user_id": user_id},
        ).scalar_one()
        if child_count or ref_count:
            raise ValueError("classification is referenced; disable or migrate it first")
        session.execute(
            text("DELETE FROM classification_nodes WHERE id = :id AND owner_user_id = :user_id"),
            {"id": node_id, "user_id": user_id},
        )

    def children(
        self, session: Session, *, node_id: str, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        if self.get_node(session, node_id=node_id, user_id=user_id) is None:
            raise ValueError("classification not found")
        return [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT id, domain_id, parent_id, level, name, description, path,
                           sort_order, status
                    FROM classification_nodes
                    WHERE parent_id = :parent_id AND owner_user_id = :user_id
                    ORDER BY sort_order, name, id LIMIT :limit OFFSET :offset
                    """
                ),
                {"parent_id": node_id, "user_id": user_id, "limit": limit, "offset": offset},
            ).mappings()
        ]

    def get_assignments(
        self, session: Session, *, knowledge_id: str, user_id: str
    ) -> dict[str, Any] | None:
        knowledge = (
            session.execute(
                text(
                    """
                SELECT id, primary_domain_id FROM knowledge_objects
                WHERE id = :id AND owner_user_id = :user_id
                """
                ),
                {"id": knowledge_id, "user_id": user_id},
            )
            .mappings()
            .first()
        )
        if knowledge is None:
            return None
        tags = (
            session.execute(
                text(
                    """
                SELECT kc.classification_node_id AS id, cn.domain_id, cn.parent_id,
                       cn.level, cn.name, cn.description, cn.path, cn.status,
                       kc.is_primary, kc.source, kc.confidence, kc.confirmation_status
                FROM knowledge_classifications kc
                JOIN classification_nodes cn ON cn.id = kc.classification_node_id
                WHERE kc.knowledge_object_id = :knowledge_id
                  AND cn.owner_user_id = :user_id
                ORDER BY cn.domain_id, cn.path
                """
                ),
                {"knowledge_id": knowledge_id, "user_id": user_id},
            )
            .mappings()
            .all()
        )
        return {
            "knowledge_object_id": knowledge_id,
            "primary_domain_id": knowledge["primary_domain_id"],
            "tags": [dict(row) for row in tags],
        }

    def assign(
        self,
        session: Session,
        *,
        knowledge_id: str,
        user_id: str,
        primary_domain_id: str,
        node_ids: list[str],
        request_id: str | None,
    ) -> dict[str, Any]:
        before = self.get_assignments(session, knowledge_id=knowledge_id, user_id=user_id)
        if before is None:
            raise ValueError("knowledge not found")
        domain = session.execute(
            text("SELECT id FROM domain_catalog WHERE id = :id AND status = 'active'"),
            {"id": primary_domain_id},
        ).scalar_one_or_none()
        if domain is None:
            raise ValueError("unknown or inactive primary domain")
        unique_node_ids = list(dict.fromkeys(node_ids))
        if unique_node_ids:
            rows = (
                session.execute(
                    text(
                        """
                    SELECT id, domain_id, status FROM classification_nodes
                    WHERE owner_user_id = :user_id AND id IN :ids
                    """
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"user_id": user_id, "ids": unique_node_ids},
                )
                .mappings()
                .all()
            )
            if len(rows) != len(unique_node_ids):
                raise ValueError("one or more classifications not found")
            if any(row["status"] != "active" for row in rows):
                raise ValueError("inactive classifications cannot be assigned")
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET primary_domain_id = :domain, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :user_id
                """
            ),
            {"domain": primary_domain_id, "id": knowledge_id, "user_id": user_id},
        )
        session.execute(
            text(
                """
                DELETE FROM knowledge_classifications
                WHERE knowledge_object_id = :knowledge_id
                  AND classification_node_id IN (
                    SELECT id FROM classification_nodes WHERE owner_user_id = :user_id
                  )
                """
            ),
            {"knowledge_id": knowledge_id, "user_id": user_id},
        )
        for node_id in unique_node_ids:
            session.execute(
                text(
                    """
                    INSERT INTO knowledge_classifications
                      (knowledge_object_id, classification_node_id, is_primary, source,
                       confirmation_status, created_by_user_id)
                    VALUES (:knowledge_id, :node_id, 0, 'user_confirmed', 'confirmed', :user_id)
                    """
                ),
                {"knowledge_id": knowledge_id, "node_id": node_id, "user_id": user_id},
            )
        after = self.get_assignments(session, knowledge_id=knowledge_id, user_id=user_id)
        session.execute(
            text(
                """
                INSERT INTO classification_change_events
                  (id, knowledge_object_id, actor_user_id, action, before_json, after_json,
                   source, request_id)
                VALUES (
                  :id, :knowledge_id, :user_id, 'replace', :before, :after, 'user', :request_id
                )
                """
            ),
            {
                "id": new_id(),
                "knowledge_id": knowledge_id,
                "user_id": user_id,
                "before": json_text(before),
                "after": json_text(after or {}),
                "request_id": request_id,
            },
        )
        return after or {}

    def history(
        self, session: Session, *, knowledge_id: str, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        if self.get_assignments(session, knowledge_id=knowledge_id, user_id=user_id) is None:
            raise ValueError("knowledge not found")
        rows = session.execute(
            text(
                """
                SELECT e.id, e.action, e.before_json, e.after_json, e.source, e.request_id,
                       e.created_at, e.actor_user_id
                FROM classification_change_events e
                JOIN knowledge_objects ko ON ko.id = e.knowledge_object_id
                WHERE e.knowledge_object_id = :knowledge_id AND ko.owner_user_id = :user_id
                ORDER BY e.created_at DESC, e.id DESC LIMIT :limit OFFSET :offset
                """
            ),
            {"knowledge_id": knowledge_id, "user_id": user_id, "limit": limit, "offset": offset},
        ).mappings()
        return [
            {
                **dict(row),
                "before": json.loads(str(row["before_json"])),
                "after": json.loads(str(row["after_json"])),
            }
            for row in rows
        ]

    def unclassified(
        self, session: Session, *, user_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT ko.id AS knowledge_object_id, ko.title, ko.primary_domain_id,
                       ko.lifecycle_status, ko.object_kind, ko.updated_at
                FROM knowledge_objects ko
                WHERE ko.owner_user_id = :user_id
                  AND ko.lifecycle_status <> 'privacy_erased'
                  AND NOT EXISTS (
                    SELECT 1 FROM knowledge_classifications kc
                    WHERE kc.knowledge_object_id = ko.id
                      AND kc.confirmation_status = 'confirmed'
                  )
                ORDER BY ko.updated_at DESC, ko.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"user_id": user_id, "limit": limit, "offset": offset},
        ).mappings()
        return [dict(row) for row in rows]


__all__ = ["ClassificationNode", "ClassificationRepository"]
