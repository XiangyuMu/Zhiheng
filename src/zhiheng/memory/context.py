from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from zhiheng.memory.repository import (
    L0_ALLOWED_PREFIXES,
    L0_MAX_ITEMS,
    L0_MAX_SERIALIZED_BYTES,
)

MemoryLayer = Literal["L0", "L1"]

LIMIT_POLICY_VERSION = "memory-context-v1"
L1_MAX_ITEMS = 32
MAX_TOTAL_SERIALIZED_BYTES = L0_MAX_SERIALIZED_BYTES * 2
MAX_ENTRY_SERIALIZED_BYTES = 4096
TOPIC_PREFIX_PATTERN = r"^[a-z][a-z0-9]*(?:\.[a-z0-9]+)*\.$"


@dataclass(frozen=True)
class MemoryContextEntry:
    formal_memory_id: str
    formal_version_id: str
    confirmation_generation: int
    state_key: str
    layer: MemoryLayer
    value_json: str
    memory_type: str
    origin_kind: str
    source_kind: str
    sensitivity_level: str
    confidence: float
    valid_from: str
    valid_to: str | None

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "confirmation_generation": self.confirmation_generation,
            "formal_memory_id": self.formal_memory_id,
            "formal_version_id": self.formal_version_id,
            "layer": self.layer,
            "memory_type": self.memory_type,
            "origin_kind": self.origin_kind,
            "sensitivity_level": self.sensitivity_level,
            "source_kind": self.source_kind,
            "state_key": self.state_key,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "value_json": self.value_json,
        }


@dataclass(frozen=True)
class MemoryContextSnapshot:
    query_hash: str
    topic_prefix: str | None
    entries: tuple[MemoryContextEntry, ...]
    digest: str
    truncated: bool
    omitted_count: int
    eligible_count: int
    source_digest: str
    limit_policy_version: str = LIMIT_POLICY_VERSION

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "eligible_count": self.eligible_count,
            "entries": [entry.canonical_payload() for entry in self.entries],
            "limit_policy_version": self.limit_policy_version,
            "omitted_count": self.omitted_count,
            "query_hash": self.query_hash,
            "source_digest": self.source_digest,
            "topic_prefix": self.topic_prefix,
            "truncated": self.truncated,
        }


class MemoryContextService:
    def load(
        self,
        session: Session,
        *,
        query_hash: str,
        topic_prefix: str | None = None,
        query: str | None = None,
        intent: str | None = None,
    ) -> MemoryContextSnapshot:
        if not _is_sha256_hex(query_hash):
            raise ValueError("query_hash must be a sha256 hex digest")
        normalized_prefix = _normalize_topic_prefix(topic_prefix)
        if normalized_prefix is None and query:
            normalized_prefix = select_l1_topic(query, intent=intent)
        candidates = _candidate_entries(
            session,
            topic_prefix=normalized_prefix,
        )
        source_digest = _source_digest(candidates)
        entries: list[MemoryContextEntry] = []
        omitted_count = 0
        metadata = {
            "eligible_count": len(candidates),
            "limit_policy_version": LIMIT_POLICY_VERSION,
            "query_hash": query_hash,
            "source_digest": source_digest,
            "topic_prefix": normalized_prefix,
        }
        l0_count = 0
        l1_count = 0
        seen_formal_ids: set[str] = set()

        for entry in candidates:
            if entry.formal_memory_id in seen_formal_ids:
                omitted_count += 1
                continue
            if entry.layer == "L0":
                if l0_count >= L0_MAX_ITEMS:
                    omitted_count += 1
                    continue
            elif l1_count >= L1_MAX_ITEMS:
                omitted_count += 1
                continue

            entry_size = _payload_size(entry.canonical_payload())
            if entry_size > MAX_ENTRY_SERIALIZED_BYTES:
                omitted_count += 1
                continue
            prospective_entries = (*entries, entry)
            if (
                entry.layer == "L0"
                and _payload_size(
                    {
                        "entries": [
                            item.canonical_payload()
                            for item in prospective_entries
                            if item.layer == "L0"
                        ],
                    }
                )
                > L0_MAX_SERIALIZED_BYTES
            ):
                omitted_count += 1
                continue
            next_total = _payload_size(
                {
                    **metadata,
                    "entries": [item.canonical_payload() for item in prospective_entries],
                    "digest": "0" * 64,
                    # Reserve the worst-case count/framing, not just item bytes.
                    "omitted_count": len(candidates),
                    "truncated": False,
                }
            )
            if next_total > MAX_TOTAL_SERIALIZED_BYTES:
                omitted_count += 1
                continue

            entries.append(entry)
            seen_formal_ids.add(entry.formal_memory_id)
            if entry.layer == "L0":
                l0_count += 1
            else:
                l1_count += 1

        truncated = omitted_count > 0
        digest_payload = {
            **metadata,
            "entries": [entry.canonical_payload() for entry in entries],
            "omitted_count": omitted_count,
            "truncated": truncated,
        }
        return MemoryContextSnapshot(
            query_hash=query_hash,
            topic_prefix=normalized_prefix,
            entries=tuple(entries),
            digest=_sha256_json(digest_payload),
            truncated=truncated,
            omitted_count=omitted_count,
            eligible_count=len(candidates),
            source_digest=source_digest,
        )

    def validate(
        self,
        session: Session,
        snapshot: MemoryContextSnapshot,
        *,
        query_hash: str,
    ) -> bool:
        if (
            snapshot.query_hash != query_hash
            or snapshot.limit_policy_version != LIMIT_POLICY_VERSION
        ):
            return False
        try:
            current = self.load(session, query_hash=query_hash, topic_prefix=snapshot.topic_prefix)
        except ValueError:
            return False
        return current.canonical_payload() == snapshot.canonical_payload()


def select_l1_topic(query: str, *, intent: str | None = None) -> str | None:
    """Select a bounded formal namespace using deterministic topic cues."""
    text = query.lower()
    if any(x in text for x in ("面试", "interview", "求职", "简历")):
        return "project.career."
    if any(x in text for x in ("agent", "rag", "检索", "向量", "hybrid")):
        return "project.agent."
    if intent in {"decision", "complex_synthesis"}:
        return "project."
    return None


def _candidate_entries(
    session: Session,
    *,
    topic_prefix: str | None,
) -> list[MemoryContextEntry]:
    rows = session.execute(
        text(
            """
            SELECT
              id,
              memory_type,
              state_key,
              current_version_id,
              current_generation,
              sensitivity_level,
              confidence,
              valid_from,
              valid_to,
              origin_kind,
              version_source_kind,
              value_json,
              effective_generation
            FROM (
              SELECT cfm.*, fmv.source_kind AS version_source_kind
              FROM current_formal_memory cfm
              JOIN formal_memory_versions fmv ON fmv.id = cfm.current_version_id
            )
            WHERE datetime(valid_from) <= datetime('now')
              AND (valid_to IS NULL OR datetime(valid_to) > datetime('now'))
              AND (
                state_key LIKE 'identity.%'
                OR state_key LIKE 'role.%'
                OR state_key LIKE 'goal.%'
                OR state_key LIKE 'project.%'
                OR state_key LIKE 'constraint.%'
                OR (
                  memory_type = 'preference'
                  AND state_key LIKE 'safety.%'
                )
                OR (:topic_prefix IS NOT NULL AND state_key >= :topic_prefix
                  AND state_key < :topic_prefix_upper)
              )
            """
        ),
        {
            "topic_prefix": topic_prefix,
            "topic_prefix_upper": _prefix_upper_bound(topic_prefix),
        },
    ).mappings()
    entries = [_entry_from_row(row, topic_prefix=topic_prefix) for row in rows]
    entries.sort(key=_entry_sort_key)
    return entries


def _entry_from_row(row: RowMapping, *, topic_prefix: str | None) -> MemoryContextEntry:
    state_key = str(row["state_key"])
    memory_type = str(row["memory_type"])
    layer: MemoryLayer = "L0" if _is_l0_allowed(state_key, memory_type) else "L1"
    if layer == "L1" and topic_prefix is None:
        raise ValueError("L1 entry requires topic scope")
    return MemoryContextEntry(
        formal_memory_id=str(row["id"]),
        formal_version_id=str(row["current_version_id"]),
        confirmation_generation=int(row["effective_generation"]),
        state_key=state_key,
        layer=layer,
        value_json=_canonical_value_json(row["value_json"]),
        memory_type=memory_type,
        origin_kind=str(row["origin_kind"]),
        source_kind=str(row["version_source_kind"]),
        sensitivity_level=str(row["sensitivity_level"]),
        confidence=float(row["confidence"]),
        valid_from=str(row["valid_from"]),
        valid_to=None if row["valid_to"] is None else str(row["valid_to"]),
    )


def _entry_sort_key(entry: MemoryContextEntry) -> tuple[int, int, str, str]:
    return (
        0 if entry.layer == "L0" else 1,
        _l0_rank(entry.state_key, entry.memory_type),
        entry.state_key,
        entry.formal_memory_id,
    )


def _l0_rank(state_key: str, memory_type: str) -> int:
    prefixes = ("identity.", "role.", "goal.", "project.", "safety.", "constraint.")
    for index, prefix in enumerate(prefixes, start=1):
        if state_key.startswith(prefix):
            return index
    if memory_type == "preference" and state_key.startswith("safety."):
        return 5
    return 99


def _is_l0_allowed(state_key: str, memory_type: str) -> bool:
    if state_key.startswith(L0_ALLOWED_PREFIXES):
        return True
    return memory_type == "preference" and state_key.startswith("safety.")


def _normalize_topic_prefix(topic_prefix: str | None) -> str | None:
    if topic_prefix is None:
        return None
    if len(topic_prefix) > 128 or re.fullmatch(TOPIC_PREFIX_PATTERN, topic_prefix) is None:
        raise ValueError("topic_prefix must be a bounded literal dotted namespace")
    return topic_prefix


def _prefix_upper_bound(prefix: str | None) -> str | None:
    if prefix is None:
        return None
    return f"{prefix}\uffff"


def _canonical_value_json(value: object) -> str:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise TypeError("memory value_json must be a JSON object")
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_digest(entries: list[MemoryContextEntry]) -> str:
    return _sha256_json(
        {
            "entries": [
                {
                    "confirmation_generation": entry.confirmation_generation,
                    "formal_memory_id": entry.formal_memory_id,
                    "formal_version_id": entry.formal_version_id,
                    "state_key": entry.state_key,
                    "value_hash": hashlib.sha256(entry.value_json.encode("utf-8")).hexdigest(),
                }
                for entry in entries
            ],
            "limit_policy_version": LIMIT_POLICY_VERSION,
        }
    )


def _payload_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _sha256_json(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
