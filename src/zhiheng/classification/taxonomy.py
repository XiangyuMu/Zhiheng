from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json


@dataclass(frozen=True, slots=True)
class DomainDefinition:
    id: str
    name: str
    description: str
    sort_order: int


# These identifiers are part of the persisted contract. Labels can evolve while
# an identifier remains resolvable for history and citations.
PRIMARY_DOMAINS: tuple[DomainDefinition, ...] = (
    DomainDefinition(
        "mathematics_formal_sciences", "数学与形式科学", "数学、逻辑、概率与统计基础", 10
    ),
    DomainDefinition("natural_sciences", "自然科学", "物理、化学、生物、地球与宇宙科学", 20),
    DomainDefinition(
        "computing_engineering", "计算机与工程技术", "计算机、AI、软件、电子及其他工程", 30
    ),
    DomainDefinition("medicine_health", "医学与健康", "医学、营养、运动、睡眠与心理健康", 40),
    DomainDefinition("psychology_cognition", "心理与认知", "感知、情绪、动机、认知及行为机制", 50),
    DomainDefinition(
        "society_politics_law", "社会、政治与法律", "社会结构、公共政策、政治、法律与制度", 60
    ),
    DomainDefinition(
        "economics_finance_business", "经济、金融与商业", "经济学、投资、个人财务与商业管理", 70
    ),
    DomainDefinition(
        "history_philosophy_religion", "历史、哲学与宗教", "历史解释、哲学思想、伦理与宗教", 80
    ),
    DomainDefinition(
        "language_literature_arts", "语言、文学与艺术", "语言、文学、音乐、视觉艺术与创作", 90
    ),
    DomainDefinition(
        "education_learning", "教育与学习", "教育方法、学习策略、知识管理与技能习得", 100
    ),
    DomainDefinition(
        "career_work_practice", "职业与工作实践", "职业选择、求职、工作协作与个人工作方法", 110
    ),
    DomainDefinition(
        "relationships_communication", "人际关系与沟通", "亲密关系、家庭、社交、沟通与冲突处理", 120
    ),
    DomainDefinition(
        "lifestyle_daily_life", "生活方式与日常事务", "居家、穿搭、饮食、出行与日常安排", 130
    ),
    DomainDefinition(
        "sports_games_leisure", "体育、游戏与休闲", "运动项目、竞技、游戏规则与休闲活动", 140
    ),
)

RECORD_TYPES: tuple[dict[str, str], ...] = (
    {"id": "knowledge", "name": "知识条目", "description": "可独立理解的主题知识"},
    {
        "id": "personal_archive_experience",
        "name": "个人档案与经历",
        "description": "个人经历、决策、反思与结果的记录类型",
    },
)

LEGACY_DOMAIN_MAPPING: dict[str, tuple[str, ...]] = {
    "academic_career": ("education_learning", "career_work_practice"),
    "technology_engineering": ("computing_engineering",),
    "finance_assets": ("economics_finance_business",),
    "society_public_issues": ("society_politics_law",),
    "learning_personal_development": ("education_learning",),
    "health_wellbeing": ("medicine_health",),
    "relationships_communication": ("relationships_communication",),
    "lifestyle_aesthetics": ("lifestyle_daily_life",),
    "arts_creation": ("language_literature_arts",),
    "personal_archive_experience": tuple(domain.id for domain in PRIMARY_DOMAINS),
}


def domain_dict(domain: DomainDefinition) -> dict[str, Any]:
    return {
        "id": domain.id,
        "name": domain.name,
        "description": domain.description,
        "sort_order": domain.sort_order,
        "is_primary": True,
        "is_legacy": False,
    }


def proposal_etag(payload: Mapping[str, Any]) -> str:
    canonical = {
        key: payload.get(key)
        for key in (
            "id",
            "proposal_type",
            "target_id",
            "status",
            "base_revision",
            "payload",
            "preview",
        )
    }
    return f"taxonomy:{sha256_json(canonical)}"


class TaxonomyRepository:
    def list_taxonomy(self, session: Session, *, user_id: str) -> dict[str, Any]:
        domains = [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT id, name, description, sort_order, schema_version, status
                    FROM domain_catalog
                    ORDER BY sort_order, id
                    """
                )
            ).mappings()
        ]
        domain_ids = {domain.id for domain in PRIMARY_DOMAINS}
        for item in domains:
            item["is_primary"] = str(item["id"]) in domain_ids
            item["is_legacy"] = not item["is_primary"]
        return {
            "taxonomy_version": "v2",
            "domains": domains,
            "primary_domains": [item for item in domains if item["is_primary"]],
            "legacy_domains": [item for item in domains if item["is_legacy"]],
            "record_types": [dict(item) for item in RECORD_TYPES],
            "legacy_mapping": {key: list(value) for key, value in LEGACY_DOMAIN_MAPPING.items()},
        }

    def knowledge_assignment(
        self, session: Session, *, user_id: str, knowledge_id: str
    ) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, primary_domain_id, record_type, classification_revision,
                       title, object_kind, lifecycle_status
                FROM knowledge_objects
                WHERE id = :id AND owner_user_id = :owner
                """
                ),
                {"id": knowledge_id, "owner": user_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def create_reclassification_proposal(
        self,
        session: Session,
        *,
        user_id: str,
        knowledge_id: str,
        primary_domain_id: str,
        record_type: str = "knowledge",
        classification_node_ids: list[str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        current = self.knowledge_assignment(session, user_id=user_id, knowledge_id=knowledge_id)
        if current is None:
            raise ValueError("knowledge not found")
        self._require_domain(session, primary_domain_id)
        self._require_record_type(record_type)
        nodes = self._validate_nodes(
            session,
            user_id=user_id,
            node_ids=classification_node_ids or [],
            domain_id=primary_domain_id,
        )
        before = {
            "primary_domain_id": str(current["primary_domain_id"]),
            "record_type": str(current["record_type"] or "knowledge"),
            "classification_revision": int(current["classification_revision"] or 0),
        }
        after = {
            "primary_domain_id": primary_domain_id,
            "record_type": record_type,
            "classification_node_ids": nodes,
        }
        preview = {
            "kind": "knowledge_reclassification",
            "knowledge_object_id": knowledge_id,
            "title": str(current["title"]),
            "before": before,
            "after": after,
            "diff": {
                "primary_domain_id": [before["primary_domain_id"], primary_domain_id],
                "record_type": [before["record_type"], record_type],
            },
        }
        return self._insert_proposal(
            session,
            user_id=user_id,
            proposal_type="knowledge_reclassification",
            target_id=knowledge_id,
            base_revision=int(str(before["classification_revision"])),
            payload={"before": before, "after": after, "reason": reason},
            preview=preview,
        )

    def create_domain_proposal(
        self,
        session: Session,
        *,
        user_id: str,
        operation: str,
        reason: str,
        domain: dict[str, Any] | None = None,
        source_domain_ids: list[str] | None = None,
        new_domains: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if operation not in {"add", "merge", "split"}:
            raise ValueError("operation must be add, merge, or split")
        source_ids = list(dict.fromkeys(source_domain_ids or []))
        for source_id in source_ids:
            row = session.execute(
                text("SELECT id FROM domain_catalog WHERE id = :id"),
                {"id": source_id},
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("source domain not found")
        requested_domains = list(new_domains or ([] if domain is None else [domain]))
        if operation == "add" and len(requested_domains) != 1:
            raise ValueError("add requires one domain")
        if operation == "merge" and (len(source_ids) < 2 or len(requested_domains) != 1):
            raise ValueError("merge requires at least two source domains and one target")
        if operation == "split" and (len(source_ids) != 1 or len(requested_domains) < 2):
            raise ValueError("split requires one source domain and at least two targets")
        for item in requested_domains:
            self._validate_domain_payload(item)
            existing = session.execute(
                text("SELECT id FROM domain_catalog WHERE id = :id"),
                {"id": item["id"]},
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError("new domain id already exists")
        preview = {
            "kind": "domain_structure",
            "operation": operation,
            "reason": reason,
            "source_domain_ids": source_ids,
            "new_domains": requested_domains,
            "affected_knowledge": self._affected_knowledge(
                session, user_id=user_id, domain_ids=source_ids
            ),
        }
        return self._insert_proposal(
            session,
            user_id=user_id,
            proposal_type="domain_structure",
            target_id=None,
            base_revision=None,
            payload=preview,
            preview=preview,
        )

    def create_legacy_migration_proposal(
        self,
        session: Session,
        *,
        user_id: str,
        mapping: dict[str, str],
        reason: str = "",
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        rows = session.execute(
            text(
                """
                SELECT id, title, primary_domain_id, record_type, classification_revision
                FROM knowledge_objects
                WHERE owner_user_id = :owner
                  AND primary_domain_id = 'personal_archive_experience'
                  AND lifecycle_status <> 'privacy_erased'
                ORDER BY created_at, id
                """
            ),
            {"owner": user_id},
        ).mappings()
        for row in rows:
            target = mapping.get(str(row["id"]))
            if target is None:
                continue
            self._require_domain(session, target)
            items.append(
                {
                    "knowledge_object_id": str(row["id"]),
                    "title": str(row["title"]),
                    "before": {
                        "primary_domain_id": str(row["primary_domain_id"]),
                        "record_type": str(row["record_type"] or "knowledge"),
                        "classification_revision": int(row["classification_revision"] or 0),
                    },
                    "after": {
                        "primary_domain_id": target,
                        "record_type": "personal_archive_experience",
                    },
                }
            )
        preview = {
            "kind": "legacy_migration",
            "legacy_domain_id": "personal_archive_experience",
            "items": items,
            "reason": reason,
            "approved_item_count": len(items),
        }
        return self._insert_proposal(
            session,
            user_id=user_id,
            proposal_type="legacy_migration",
            target_id="personal_archive_experience",
            base_revision=None,
            payload={"mapping": mapping, "reason": reason},
            preview=preview,
        )

    def list_proposals(
        self, session: Session, *, user_id: str, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, proposal_type, target_id, status, base_revision, payload_json,
                   preview_json, created_at, updated_at, decided_at, decided_by_user_id
            FROM taxonomy_proposals
            WHERE owner_user_id = :owner
        """
        params: dict[str, Any] = {"owner": user_id, "limit": limit}
        if status:
            query += " AND status = :status"
            params["status"] = status
        query += " ORDER BY created_at DESC, id DESC LIMIT :limit"
        return [self._proposal_row(row) for row in session.execute(text(query), params).mappings()]

    def get_proposal(
        self, session: Session, *, user_id: str, proposal_id: str
    ) -> dict[str, Any] | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, proposal_type, target_id, status, base_revision, payload_json,
                       preview_json, created_at, updated_at, decided_at, decided_by_user_id
                FROM taxonomy_proposals WHERE id = :id AND owner_user_id = :owner
                """
                ),
                {"id": proposal_id, "owner": user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = self._proposal_row(row)
        result["etag"] = proposal_etag(result)
        return result

    def approve_proposal(
        self,
        session: Session,
        *,
        user_id: str,
        proposal_id: str,
        expected_etag: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        proposal = self.get_proposal(session, user_id=user_id, proposal_id=proposal_id)
        if proposal is None:
            raise ValueError("taxonomy proposal not found")
        if proposal["status"] != "pending":
            return proposal
        if expected_etag != proposal["etag"]:
            raise ValueError("stale taxonomy proposal")
        proposal_type = str(proposal["proposal_type"])
        if proposal_type == "knowledge_reclassification":
            result = self._approve_reclassification(
                session,
                user_id=user_id,
                proposal=proposal,
                request_id=request_id,
            )
        elif proposal_type in {"domain_structure", "legacy_migration"}:
            result = self._approve_domain_structure(session, user_id=user_id, proposal=proposal)
        else:
            raise ValueError("unsupported taxonomy proposal")
        completed = proposal_type not in {"domain_structure", "legacy_migration"} or (
            not result.get("deferred") and not result.get("conflicts")
        )
        if completed:
            session.execute(
                text(
                    """
                    UPDATE taxonomy_proposals
                    SET status = 'approved', decided_at = CURRENT_TIMESTAMP,
                        decided_by_user_id = :user, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND owner_user_id = :owner AND status = 'pending'
                    """
                ),
                {"id": proposal_id, "owner": user_id, "user": user_id},
            )
        session.execute(
            text(
                """
                INSERT INTO taxonomy_proposal_events
                  (id, proposal_id, owner_user_id, action, payload_json)
                VALUES (:id, :proposal_id, :owner, :action, :payload)
                """
            ),
            {
                "id": new_id(),
                "proposal_id": proposal_id,
                "owner": user_id,
                "action": "approved" if completed else "partially_approved",
                "payload": json_text(result),
            },
        )
        return {
            "proposal_id": proposal_id,
            "status": "approved" if completed else "pending",
            "result": result,
        }

    def update_domain_migration(
        self,
        session: Session,
        *,
        user_id: str,
        proposal_id: str,
        knowledge_id: str,
        target_domain_id: str | None,
        expected_etag: str,
    ) -> dict[str, Any]:
        proposal = self.get_proposal(session, user_id=user_id, proposal_id=proposal_id)
        if proposal is None or proposal["proposal_type"] not in {
            "domain_structure",
            "legacy_migration",
        }:
            raise ValueError("taxonomy proposal not found")
        if proposal["status"] != "pending":
            raise ValueError("taxonomy proposal is no longer pending")
        if expected_etag != proposal["etag"]:
            raise ValueError("stale taxonomy proposal")
        payload = dict(proposal["payload"])
        if proposal["proposal_type"] == "domain_structure":
            allowed = {str(item["id"]) for item in payload["new_domains"]}
        else:
            allowed = {
                str(row["id"])
                for row in session.execute(
                    text("SELECT id FROM domain_catalog WHERE status = 'active'")
                ).mappings()
            }
        if target_domain_id is not None and target_domain_id not in allowed:
            raise ValueError("migration target must be one of the proposed domains")
        source_items = proposal["preview"].get(
            "affected_knowledge", proposal["preview"].get("items", [])
        )
        items = [dict(item) for item in source_items]
        for item in items:
            before = item.get("before", {})
            item.setdefault("id", item.get("knowledge_object_id"))
            item.setdefault("primary_domain_id", before.get("primary_domain_id"))
            item.setdefault("record_type", before.get("record_type", "knowledge"))
            item.setdefault("classification_revision", before.get("classification_revision", 0))
        selected = next((item for item in items if str(item["id"]) == knowledge_id), None)
        if selected is None:
            raise ValueError("knowledge is not affected by this proposal")
        selected["migration_status"] = "selected" if target_domain_id else "deferred"
        selected["target_domain_id"] = target_domain_id
        payload["decisions"] = {
            str(item["id"]): item.get("target_domain_id")
            for item in items
            if item.get("target_domain_id") is not None
        }
        preview = dict(proposal["preview"])
        preview_key = (
            "affected_knowledge" if proposal["proposal_type"] == "domain_structure" else "items"
        )
        preview[preview_key] = items
        session.execute(
            text(
                "UPDATE taxonomy_proposals SET payload_json=:payload, preview_json=:preview, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=:id AND owner_user_id=:owner "
                "AND status='pending'"
            ),
            {
                "id": proposal_id,
                "owner": user_id,
                "payload": json_text(payload),
                "preview": json_text(preview),
            },
        )
        updated = dict(proposal)
        updated["payload"] = payload
        updated["preview"] = preview
        updated["etag"] = proposal_etag(updated)
        return updated

    def _approve_reclassification(
        self, session: Session, *, user_id: str, proposal: dict[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        payload = proposal["payload"]
        before = payload["before"]
        after = payload["after"]
        current = self.knowledge_assignment(
            session, user_id=user_id, knowledge_id=str(proposal["target_id"])
        )
        if current is None:
            raise ValueError("knowledge not found")
        if int(current["classification_revision"] or 0) != int(before["classification_revision"]):
            raise ValueError("knowledge classification changed after preview")
        if str(current["primary_domain_id"]) != str(before["primary_domain_id"]):
            raise ValueError("knowledge classification changed after preview")
        self._apply_assignment(
            session,
            user_id=user_id,
            knowledge_id=str(proposal["target_id"]),
            primary_domain_id=str(after["primary_domain_id"]),
            record_type=str(after["record_type"]),
            node_ids=list(after.get("classification_node_ids") or []),
            request_id=request_id,
            action="proposal_approved",
            before=before,
        )
        return (
            self.knowledge_assignment(
                session, user_id=user_id, knowledge_id=str(proposal["target_id"])
            )
            or {}
        )

    def _approve_legacy_migration(
        self, session: Session, *, user_id: str, proposal: dict[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        applied = 0
        skipped: list[str] = []
        for item in proposal["preview"]["items"]:
            current = self.knowledge_assignment(
                session, user_id=user_id, knowledge_id=str(item["knowledge_object_id"])
            )
            if (
                current is None
                or str(current["primary_domain_id"]) != "personal_archive_experience"
                or int(current["classification_revision"] or 0)
                != int(item["before"]["classification_revision"])
            ):
                skipped.append(str(item["knowledge_object_id"]))
                continue
            after = item["after"]
            self._apply_assignment(
                session,
                user_id=user_id,
                knowledge_id=str(item["knowledge_object_id"]),
                primary_domain_id=str(after["primary_domain_id"]),
                record_type="personal_archive_experience",
                node_ids=[],
                request_id=request_id,
                action="legacy_migration_approved",
                before=item["before"],
            )
            applied += 1
        return {"applied": applied, "skipped": skipped}

    def _approve_domain_structure(
        self, session: Session, *, user_id: str, proposal: dict[str, Any]
    ) -> dict[str, Any]:
        payload = proposal["payload"]
        created: list[str] = []
        for item in payload.get("new_domains", []):
            existing = session.execute(
                text("SELECT id FROM domain_catalog WHERE id = :id"),
                {"id": item["id"]},
            ).scalar_one_or_none()
            if existing is None:
                session.execute(
                    text(
                        """
                        INSERT INTO domain_catalog
                          (id, name, description, sort_order, schema_version, status)
                        VALUES (:id, :name, :description, :sort_order, 2, 'active')
                        """
                    ),
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "description": item.get("description", ""),
                        "sort_order": int(item.get("sort_order", 1000)),
                    },
                )
                created.append(str(item["id"]))
        applied: list[str] = []
        deferred: list[str] = []
        affected = proposal["preview"].get(
            "affected_knowledge", proposal["preview"].get("items", [])
        )
        for item in affected:
            before = item.get("before", {})
            item.setdefault("id", item.get("knowledge_object_id"))
            item.setdefault("primary_domain_id", before.get("primary_domain_id"))
            item.setdefault("record_type", before.get("record_type", "knowledge"))
            item.setdefault("classification_revision", before.get("classification_revision", 0))
        decisions = payload.get("decisions", {})
        conflicts: list[str] = []
        for item in affected:
            knowledge_id = str(item["id"])
            if item.get("migration_status") == "applied":
                continue
            target = decisions.get(knowledge_id)
            if target is None:
                deferred.append(knowledge_id)
                continue
            current = self.knowledge_assignment(session, user_id=user_id, knowledge_id=knowledge_id)
            if (
                current is None
                or str(current["primary_domain_id"]) != str(item["primary_domain_id"])
                or int(current["classification_revision"] or 0)
                != int(item["classification_revision"] or 0)
            ):
                conflicts.append(knowledge_id)
                continue
            self._require_domain(session, str(target))
            node_ids = self._assignment_node_ids(
                session, user_id=user_id, knowledge_id=knowledge_id
            )
            self._apply_assignment(
                session,
                user_id=user_id,
                knowledge_id=knowledge_id,
                primary_domain_id=(
                    str(current["primary_domain_id"])
                    if int(item.get("association_only", 0))
                    else str(target)
                ),
                record_type=str(item["record_type"] or "knowledge"),
                node_ids=node_ids,
                request_id=None,
                action="domain_migration_approved",
                before={
                    "primary_domain_id": str(item["primary_domain_id"]),
                    "record_type": str(item["record_type"] or "knowledge"),
                    "classification_revision": int(item["classification_revision"] or 0),
                    "classification_node_ids": node_ids,
                },
            )
            item["migration_status"] = "applied"
            decisions.pop(knowledge_id, None)
            applied.append(knowledge_id)
        source_domains = list(payload.get("source_domain_ids", []))
        if source_domains and not self._affected_knowledge(
            session, user_id=user_id, domain_ids=source_domains
        ):
            self._disable_domains(session, source_domains)
        preview_key = (
            "affected_knowledge" if "affected_knowledge" in proposal["preview"] else "items"
        )
        session.execute(
            text(
                "UPDATE taxonomy_proposals SET payload_json=:payload, preview_json=:preview, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=:id AND owner_user_id=:owner"
            ),
            {
                "id": proposal["id"],
                "owner": user_id,
                "payload": json_text(payload),
                "preview": json_text({**proposal["preview"], preview_key: affected}),
            },
        )
        return {
            "operation": payload.get("operation", "legacy_migration"),
            "created_domain_ids": created,
            "applied": applied,
            "deferred": deferred,
            "conflicts": conflicts,
        }

    def _apply_assignment(
        self,
        session: Session,
        *,
        user_id: str,
        knowledge_id: str,
        primary_domain_id: str,
        record_type: str,
        node_ids: list[str],
        request_id: str | None,
        action: str,
        before: dict[str, Any],
    ) -> None:
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET primary_domain_id = :domain, record_type = :record_type,
                    classification_revision = classification_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :owner
                """
            ),
            {
                "domain": primary_domain_id,
                "record_type": record_type,
                "id": knowledge_id,
                "owner": user_id,
            },
        )

        session.execute(
            text(
                """
                DELETE FROM knowledge_classifications
                WHERE knowledge_object_id = :knowledge_id
                  AND classification_node_id IN (
                    SELECT id FROM classification_nodes WHERE owner_user_id = :owner
                  )
                """
            ),
            {"knowledge_id": knowledge_id, "owner": user_id},
        )
        for node_id in node_ids:
            session.execute(
                text(
                    """
                    INSERT INTO knowledge_classifications
                      (knowledge_object_id, classification_node_id, is_primary, source,
                       confirmation_status, created_by_user_id)
                    VALUES (:knowledge_id, :node_id, 0, 'user_confirmed', 'confirmed', :user)
                    """
                ),
                {"knowledge_id": knowledge_id, "node_id": node_id, "user": user_id},
            )
        session.execute(
            text(
                """
                INSERT INTO classification_change_events
                  (id, knowledge_object_id, actor_user_id, action, before_json, after_json,
                   source, request_id)
                VALUES (:id, :knowledge_id, :user, :action, :before, :after,
                        'taxonomy_proposal', :request_id)
                """
            ),
            {
                "id": new_id(),
                "knowledge_id": knowledge_id,
                "user": user_id,
                "action": action,
                "before": json_text(before),
                "after": json_text(
                    {
                        "primary_domain_id": primary_domain_id,
                        "record_type": record_type,
                        "classification_node_ids": node_ids,
                    }
                ),
                "request_id": request_id,
            },
        )

    @staticmethod
    def _assignment_node_ids(session: Session, *, user_id: str, knowledge_id: str) -> list[str]:
        return [
            str(row["classification_node_id"])
            for row in session.execute(
                text(
                    """
                    SELECT kc.classification_node_id
                    FROM knowledge_classifications kc
                    JOIN classification_nodes cn ON cn.id = kc.classification_node_id
                    WHERE kc.knowledge_object_id = :knowledge_id
                      AND cn.owner_user_id = :owner
                    ORDER BY kc.classification_node_id
                    """
                ),
                {"knowledge_id": knowledge_id, "owner": user_id},
            ).mappings()
        ]

    def _insert_proposal(
        self,
        session: Session,
        *,
        user_id: str,
        proposal_type: str,
        target_id: str | None,
        base_revision: int | None,
        payload: dict[str, Any],
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        proposal_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO taxonomy_proposals
                  (id, owner_user_id, proposal_type, target_id, status, base_revision,
                   payload_json, preview_json)
                VALUES (:id, :owner, :type, :target, 'pending', :revision, :payload, :preview)
                """
            ),
            {
                "id": proposal_id,
                "owner": user_id,
                "type": proposal_type,
                "target": target_id,
                "revision": base_revision,
                "payload": json_text(payload),
                "preview": json_text(preview),
            },
        )
        result = {
            "id": proposal_id,
            "proposal_type": proposal_type,
            "target_id": target_id,
            "status": "pending",
            "base_revision": base_revision,
            "payload": payload,
            "preview": preview,
        }
        result["etag"] = proposal_etag(result)
        return result

    @staticmethod
    def _proposal_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        result["preview"] = json.loads(str(result.pop("preview_json")))
        result["base_revision"] = (
            int(result["base_revision"]) if result["base_revision"] is not None else None
        )
        return result

    @staticmethod
    def _require_record_type(record_type: str) -> None:
        if record_type not in {item["id"] for item in RECORD_TYPES}:
            raise ValueError("unknown record type")

    @staticmethod
    def _require_domain(session: Session, domain_id: str) -> None:
        row = session.execute(
            text("SELECT id FROM domain_catalog WHERE id = :id AND status = 'active'"),
            {"id": domain_id},
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("unknown or inactive domain")

    @staticmethod
    def _validate_domain_payload(item: Mapping[str, Any]) -> None:
        for key in ("id", "name"):
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"domain {key} is required")
        if len(str(item["id"])) > 64:
            raise ValueError("domain id is too long")

    @staticmethod
    def _validate_nodes(
        session: Session, *, user_id: str, node_ids: list[str], domain_id: str
    ) -> list[str]:
        node_ids = list(dict.fromkeys(node_ids))
        if not node_ids:
            return []
        rows = (
            session.execute(
                text(
                    """
                SELECT id, domain_id, status FROM classification_nodes
                WHERE owner_user_id = :owner AND id IN ({})
                """.format(",".join(f":node_{idx}" for idx in range(len(node_ids))))
                ),
                {"owner": user_id, **{f"node_{idx}": value for idx, value in enumerate(node_ids)}},
            )
            .mappings()
            .all()
        )
        if len(rows) != len(node_ids) or any(
            str(row["domain_id"]) != domain_id or str(row["status"]) != "active" for row in rows
        ):
            raise ValueError("classification nodes must be active and belong to the primary domain")
        return node_ids

    @staticmethod
    def _affected_knowledge(
        session: Session, *, user_id: str, domain_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not domain_ids:
            return []
        placeholders = ",".join(f":domain_{idx}" for idx in range(len(domain_ids)))
        rows = session.execute(
            text(
                f"""
                SELECT id, title, primary_domain_id, record_type, classification_revision,
                       CASE WHEN primary_domain_id IN ({placeholders}) THEN 0 ELSE 1 END
                         AS association_only
                FROM knowledge_objects
                WHERE owner_user_id = :owner
                  AND lifecycle_status <> 'privacy_erased'
                  AND (
                    primary_domain_id IN ({placeholders})
                    OR EXISTS (
                      SELECT 1
                      FROM knowledge_classifications kc
                      JOIN classification_nodes cn ON cn.id = kc.classification_node_id
                      WHERE kc.knowledge_object_id = knowledge_objects.id
                        AND cn.domain_id IN ({placeholders})
                    )
                  )
                ORDER BY created_at, id
                """
            ),
            {"owner": user_id, **{f"domain_{idx}": value for idx, value in enumerate(domain_ids)}},
        ).mappings()
        return [{**dict(row), "knowledge_object_id": str(row["id"])} for row in rows]

    @staticmethod
    def _disable_domains(session: Session, domain_ids: list[str]) -> None:
        if domain_ids:
            placeholders = ",".join(f":domain_{idx}" for idx in range(len(domain_ids)))
            session.execute(
                text(
                    "UPDATE domain_catalog SET status = 'disabled', "
                    f"updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})"
                ),
                {f"domain_{idx}": value for idx, value in enumerate(domain_ids)},
            )


__all__ = [
    "DomainDefinition",
    "LEGACY_DOMAIN_MAPPING",
    "PRIMARY_DOMAINS",
    "RECORD_TYPES",
    "TaxonomyRepository",
    "domain_dict",
    "proposal_etag",
]
