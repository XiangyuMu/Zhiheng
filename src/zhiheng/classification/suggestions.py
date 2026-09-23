from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.models import ModelGateway, ModelRequest

CLASSIFICATION_SUGGESTION_JOB_TYPE = "classification.suggest"
CLASSIFICATION_SUGGESTION_EVENT = "knowledge.classification_suggestion_requested"


@dataclass(frozen=True, slots=True)
class ClassificationNode:
    id: str
    name: str
    path: str
    level: int
    parent_id: str | None = None
    domain_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClassificationCandidate:
    node_id: str
    confidence: float
    explanation: str
    source: str = "rule"


@dataclass(frozen=True, slots=True)
class ClassificationSuggestion:
    id: str
    knowledge_object_id: str
    owner_user_id: str
    candidate_node_id: str
    confidence: float
    explanation: str
    source: str
    status: str


class ClassificationSuggestionProvider(Protocol):
    def suggest(
        self,
        *,
        title: str,
        text_content: str,
        candidates: Sequence[ClassificationNode],
    ) -> Sequence[ClassificationCandidate]: ...


class RuleCandidateSelector:
    """Small deterministic pre-filter that bounds the model candidate set."""

    def __init__(self, *, max_candidates: int = 12) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self.max_candidates = max_candidates

    def select(
        self,
        nodes: Sequence[ClassificationNode],
        *,
        title: str,
        text_content: str,
        source_kind: str | None = None,
        tags: Iterable[str] = (),
    ) -> list[ClassificationNode]:
        haystack = _tokens(" ".join((title, text_content, source_kind or "", *tags)))
        scored: list[tuple[int, ClassificationNode]] = []
        for node in nodes:
            node_tokens = _tokens(" ".join((node.name, node.path)))
            score = len(haystack & node_tokens)
            if node.name.casefold() in title.casefold():
                score += 3
            if score:
                scored.append((score, node))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].id))
        selected = [node for _, node in scored[: self.max_candidates]]
        if selected:
            return selected
        return list(sorted(nodes, key=lambda node: (node.path, node.id))[: self.max_candidates])


class ModelClassificationProvider:
    """Adapter for the existing privacy audited model gateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        provider_id: str,
        model_id: str,
        task_prefix: str = "classification",
    ) -> None:
        self.gateway = gateway
        self.provider_id = provider_id
        self.model_id = model_id
        self.task_prefix = task_prefix

    def suggest(
        self,
        *,
        title: str,
        text_content: str,
        candidates: Sequence[ClassificationNode],
    ) -> Sequence[ClassificationCandidate]:
        candidate_payload = [
            {"id": node.id, "name": node.name, "path": node.path, "level": node.level}
            for node in candidates
        ]
        prompt = (
            "Return JSON only in the form "
            '{"suggestions":[{"node_id":"...","confidence":0.0,"explanation":"..."}]}. '
            "Choose only node IDs from candidates. Keep explanations under 240 characters.\n"
            f"Title: {title[:512]}\n"
            f"Content: {text_content[:6000]}\n"
            f"Candidates: {json.dumps(candidate_payload, ensure_ascii=False)}"
        )
        response = self.gateway.complete(
            ModelRequest(
                task_id=(
                    f"{self.task_prefix}:{sha256_json({'title': title, 'content': text_content})}"
                ),
                provider_id=self.provider_id,
                model_id=self.model_id,
                payload=prompt,
            )
        )
        return parse_model_suggestions(response.text, candidates)


def parse_model_suggestions(
    raw: str,
    candidates: Sequence[ClassificationNode],
) -> list[ClassificationCandidate]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid classification model output: expected JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("suggestions"), list):
        raise ValueError("invalid classification model output: suggestions must be an array")
    allowed = {node.id for node in candidates}
    result: list[ClassificationCandidate] = []
    seen: set[str] = set()
    for item in payload["suggestions"]:
        if not isinstance(item, Mapping):
            raise ValueError("invalid classification model output: suggestion must be an object")
        node_id = item.get("node_id")
        confidence = item.get("confidence")
        explanation = item.get("explanation", "")
        if (
            not isinstance(node_id, str)
            or node_id not in allowed
            or node_id in seen
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
            or not isinstance(explanation, str)
        ):
            raise ValueError("invalid classification model output: candidate schema mismatch")
        seen.add(node_id)
        result.append(
            ClassificationCandidate(
                node_id=node_id,
                confidence=float(confidence),
                explanation=explanation[:240],
                source="model",
            )
        )
    return result


class ClassificationSuggestionService:
    def __init__(
        self,
        *,
        rule_selector: RuleCandidateSelector | None = None,
        provider: ClassificationSuggestionProvider | None = None,
    ) -> None:
        self.rule_selector = rule_selector or RuleCandidateSelector()
        self.provider = provider

    def generate(
        self,
        session: Session,
        *,
        knowledge_object_id: str,
        owner_user_id: str,
        provider: ClassificationSuggestionProvider | None = None,
    ) -> list[ClassificationSuggestion]:
        knowledge = (
            session.execute(
                text(
                    """
                SELECT ko.id, ko.title, ko.owner_user_id, eo.media_type,
                       coalesce(ko.summary, '') AS summary,
                       coalesce((SELECT group_concat(sc.raw_text, char(10))
                                 FROM serving_chunks sc
                                 WHERE sc.source_id = ko.id), '') AS body
                FROM knowledge_objects ko
                LEFT JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                LEFT JOIN content_versions cv ON cv.id = kv.content_version_id
                LEFT JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE ko.id = :id AND ko.lifecycle_status <> 'privacy_erased'
                """
                ),
                {"id": knowledge_object_id},
            )
            .mappings()
            .first()
        )
        if knowledge is None:
            raise ValueError("knowledge object not found")
        if knowledge["owner_user_id"] not in {None, owner_user_id}:
            raise PermissionError("knowledge object is owned by another user")
        nodes = [
            ClassificationNode(
                id=str(row["id"]),
                name=str(row["name"]),
                path=str(row["path"]),
                level=int(row["level"]),
                parent_id=str(row["parent_id"]) if row["parent_id"] else None,
                domain_id=str(row["domain_id"]) if row["domain_id"] else None,
            )
            for row in session.execute(
                text(
                    """
                    SELECT id, name, path, level, parent_id, domain_id
                    FROM classification_nodes
                    WHERE owner_user_id = :owner AND status = 'active'
                    ORDER BY path, id
                    """
                ),
                {"owner": owner_user_id},
            ).mappings()
        ]
        selected = self.rule_selector.select(
            nodes,
            title=str(knowledge["title"]),
            text_content=f"{knowledge['summary']} {knowledge['body']}",
            source_kind=str(knowledge["media_type"]) if knowledge["media_type"] else None,
        )
        selected_provider = provider or self.provider
        candidates = list(
            selected_provider.suggest(
                title=str(knowledge["title"]),
                text_content=f"{knowledge['summary']} {knowledge['body']}",
                candidates=selected,
            )
            if selected_provider is not None
            else [
                ClassificationCandidate(
                    node_id=node.id,
                    confidence=0.35,
                    explanation="Matched by title, path, or content keywords.",
                )
                for node in selected
            ]
        )
        valid_ids = {node.id for node in selected}
        if any(candidate.node_id not in valid_ids for candidate in candidates):
            raise ValueError("classification provider returned an unknown candidate")
        result: list[ClassificationSuggestion] = []
        for candidate in candidates:
            suggestion_id = new_id()
            session.execute(
                text(
                    """
                    INSERT OR IGNORE INTO classification_suggestions
                      (id, knowledge_object_id, owner_user_id, candidate_json,
                       confidence, explanation, status, expires_at,
                       created_at, updated_at)
                    VALUES
                      (:id, :knowledge_object_id, :owner_user_id, :candidate_json,
                       :confidence, :explanation, 'pending',
                       datetime('now', '+30 days'), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "id": suggestion_id,
                    "knowledge_object_id": knowledge_object_id,
                    "owner_user_id": owner_user_id,
                    "candidate_json": json_text(
                        {"node_id": candidate.node_id, "source": candidate.source}
                    ),
                    "confidence": candidate.confidence,
                    "explanation": candidate.explanation,
                },
            )
            row = (
                session.execute(
                    text(
                        """
                    SELECT id, knowledge_object_id, owner_user_id, candidate_json,
                           confidence, explanation, status
                    FROM classification_suggestions
                    WHERE knowledge_object_id = :knowledge_object_id
                      AND owner_user_id = :owner_user_id
                      AND json_extract(candidate_json, '$.node_id') = :candidate_node_id
                      AND status = 'pending'
                    ORDER BY created_at DESC LIMIT 1
                    """
                    ),
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "owner_user_id": owner_user_id,
                        "candidate_node_id": candidate.node_id,
                    },
                )
                .mappings()
                .first()
            )
            if row:
                result.append(_suggestion_from_row(cast(Mapping[str, Any], row)))
        return result

    def accept(
        self,
        session: Session,
        *,
        suggestion_id: str,
        owner_user_id: str,
        request_id: str | None = None,
    ) -> ClassificationSuggestion:
        row = self._owned_suggestion(session, suggestion_id, owner_user_id)
        if row["status"] != "pending":
            raise ValueError("classification suggestion is no longer pending")
        session.execute(
            text(
                """
                UPDATE classification_suggestions
                SET status = 'accepted', updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :owner AND status = 'pending'
                """
            ),
            {"id": suggestion_id, "owner": owner_user_id},
        )
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO knowledge_classifications
                  (knowledge_object_id, classification_node_id, is_primary,
                   source, confidence, confirmation_status, created_by_user_id, created_at)
                VALUES (:knowledge, :node, 0, 'user_confirmed', :confidence,
                        'confirmed', :owner, CURRENT_TIMESTAMP)
                """
            ),
            {
                "knowledge": row["knowledge_object_id"],
                "node": _candidate_node_id(row),
                "confidence": row["confidence"],
                "owner": owner_user_id,
            },
        )
        if request_id:
            session.execute(
                text(
                    """
                    INSERT INTO classification_change_events
                      (id, knowledge_object_id, actor_user_id, action,
                       before_json, after_json, source, request_id, created_at)
                    VALUES (:id, :knowledge, :owner, 'suggestion_accepted',
                            :before, :after, 'user_confirmed', :request_id, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "id": new_id(),
                    "knowledge": row["knowledge_object_id"],
                    "owner": owner_user_id,
                    "before": json_text({}),
                    "after": json_text({"node_id": _candidate_node_id(row)}),
                    "request_id": request_id,
                },
            )
        updated = dict(row)
        updated["status"] = "accepted"
        return _suggestion_from_row(updated)

    def reject(
        self,
        session: Session,
        *,
        suggestion_id: str,
        owner_user_id: str,
        request_id: str | None = None,
    ) -> ClassificationSuggestion:
        row = self._owned_suggestion(session, suggestion_id, owner_user_id)
        if row["status"] != "pending":
            raise ValueError("classification suggestion is no longer pending")
        session.execute(
            text(
                """
                UPDATE classification_suggestions
                SET status = 'rejected', updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND owner_user_id = :owner AND status = 'pending'
                """
            ),
            {"id": suggestion_id, "owner": owner_user_id},
        )
        if request_id:
            session.execute(
                text(
                    """
                    INSERT INTO classification_change_events
                      (id, knowledge_object_id, actor_user_id, action,
                       before_json, after_json, source, request_id, created_at)
                    VALUES (:id, :knowledge, :owner, 'suggestion_rejected',
                            :before, :after, 'user_confirmed', :request_id, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "id": new_id(),
                    "knowledge": row["knowledge_object_id"],
                    "owner": owner_user_id,
                    "before": json_text({}),
                    "after": json_text({"node_id": _candidate_node_id(row)}),
                    "request_id": request_id,
                },
            )
        updated = dict(row)
        updated["status"] = "rejected"
        return _suggestion_from_row(updated)

    @staticmethod
    def _owned_suggestion(
        session: Session, suggestion_id: str, owner_user_id: str
    ) -> dict[str, Any]:
        row = (
            session.execute(
                text(
                    """
                    SELECT id, knowledge_object_id, owner_user_id, candidate_json,
                       confidence, explanation, status
                FROM classification_suggestions
                WHERE id = :id AND owner_user_id = :owner
                """
                ),
                {"id": suggestion_id, "owner": owner_user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("classification suggestion not found")
        return dict(row)


def enqueue_classification_suggestion(
    session: Session,
    *,
    knowledge_object_id: str,
    owner_user_id: str,
    idempotency_key: str,
) -> str:
    event_id = new_id()
    session.execute(
        text(
            """
            INSERT OR IGNORE INTO outbox_events
              (id, event_type, aggregate_type, aggregate_id, payload_json, status)
            VALUES (:id, :event_type, 'knowledge_object', :aggregate_id,
                    :payload, 'pending')
            """
        ),
        {
            "id": event_id,
            "event_type": CLASSIFICATION_SUGGESTION_EVENT,
            "aggregate_id": knowledge_object_id,
            "payload": json_text(
                {
                    "knowledge_object_id": knowledge_object_id,
                    "owner_user_id": owner_user_id,
                    "idempotency_key": idempotency_key,
                }
            ),
        },
    )
    return event_id


def _suggestion_from_row(row: Mapping[str, Any]) -> ClassificationSuggestion:
    candidate = _candidate_payload(row)
    return ClassificationSuggestion(
        id=str(row["id"]),
        knowledge_object_id=str(row["knowledge_object_id"]),
        owner_user_id=str(row["owner_user_id"]),
        candidate_node_id=str(candidate["node_id"]),
        confidence=float(row["confidence"]),
        explanation=str(row["explanation"]),
        source=str(candidate.get("source", "model")),
        status=str(row["status"]),
    )


def _candidate_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("candidate_json")
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, Mapping) or not isinstance(loaded.get("node_id"), str):
        raise ValueError("classification suggestion candidate payload is invalid")
    return dict(loaded)


def _candidate_node_id(row: Mapping[str, Any]) -> str:
    return str(_candidate_payload(row)["node_id"])


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 1
    }
