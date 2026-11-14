"""反思: weekly 综合结论草稿 drawn from formal knowledge (spec decision 23, ADR 0012).

Reflection reads formal conclusions only: never imported material, and never
a synthesized draft that is not yet approved. Formal conclusions that share a
premise are drawn together into one synthesized draft, whose basis is exactly
the formal knowledge it was drawn from. The draft is marked as system
generated and reviewed one by one like any other conclusion draft.

Each draft is keyed by the versions of its basis, so a repeated run drafts
nothing new and a rejected draft only returns once a basis entry has a new
version or new formal knowledge joins the group.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.conclusions.applicability import SYSTEM_SYNTHESIS
from zhiheng.conclusions.relations import CONSOLIDATION_KINDS, normalize_claim, relation_kind
from zhiheng.conclusions.repository import ConclusionRepository, synthesis_basis_ids
from zhiheng.consolidation.findings import iso_utc, parse_utc
from zhiheng.core.ids import json_text, new_id, sha256_json

REFLECT_EVERY = timedelta(days=7)


@dataclass(frozen=True)
class _Group:
    members: tuple[dict[str, Any], ...]
    premises: tuple[dict[str, Any], ...]

    @property
    def basis(self) -> list[dict[str, Any]]:
        return [
            {
                "entry_id": str(member["id"]),
                "version": int(member["approved_version"]),
                "title": str(member["payload"].get("title") or ""),
                "claim": str(member["payload"].get("claim") or ""),
            }
            for member in self.members
        ]

    @property
    def basis_key(self) -> str:
        return sha256_json(
            {"basis": sorted([item["entry_id"], item["version"]] for item in self.basis)}
        )


class ReflectionService:
    def __init__(self) -> None:
        self.repository = ConclusionRepository()

    def reflect_due(self, session: Session, *, now: datetime) -> int:
        """Reflect for each owner not reflected on in the last week; drafts created."""
        owners = [
            str(owner)
            for owner in session.execute(text("SELECT id FROM auth_users ORDER BY id")).scalars()
        ]
        return sum(
            self._reflect(session, owner, now=now)
            for owner in owners
            if self._due(session, owner, now=now)
        )

    @staticmethod
    def _due(session: Session, owner: str, *, now: datetime) -> bool:
        last = parse_utc(
            session.execute(
                text("SELECT max(started_at) FROM reflection_runs WHERE owner_user_id = :o"),
                {"o": owner},
            ).scalar_one()
        )
        return last is None or now - last >= REFLECT_EVERY

    def _reflect(self, session: Session, owner: str, *, now: datetime) -> int:
        created = 0
        for group in _groups(_formal_knowledge(session, owner)):
            key = group.basis_key
            if session.execute(
                text(
                    "SELECT 1 FROM reflection_syntheses WHERE owner_user_id = :o AND basis_key = :k"
                ),
                {"o": owner, "k": key},
            ).first():
                continue
            entry_id = self._draft(session, owner, group, key)
            if entry_id is None:
                continue
            session.execute(
                text(
                    "INSERT INTO reflection_syntheses "
                    "(owner_user_id, basis_key, entry_id, basis_json, created_at) "
                    "VALUES (:o, :k, :entry, :basis, :at)"
                ),
                {
                    "o": owner,
                    "k": key,
                    "entry": entry_id,
                    "basis": json_text(group.basis),
                    "at": iso_utc(now),
                },
            )
            created += 1
        session.execute(
            text(
                "INSERT INTO reflection_runs (id, owner_user_id, started_at, drafts_created) "
                "VALUES (:id, :o, :at, :n)"
            ),
            {"id": new_id(), "o": owner, "at": iso_utc(now), "n": created},
        )
        return created

    def _draft(self, session: Session, owner: str, group: _Group, key: str) -> str | None:
        basis = group.basis
        listing = "\n".join(
            f"{index}. 「{item['title']}」：{item['claim']}" for index, item in enumerate(basis, 1)
        )
        source_text = f"系统反思时从以下 {len(basis)} 条正式知识归纳：\n{listing}"
        domains = Counter(str(member["payload"].get("domain_id")) for member in group.members)
        primary = domains.most_common(1)[0][0]
        record_types = {
            str(member["payload"].get("record_type") or "knowledge") for member in group.members
        }
        payload = {
            "title": f"综合结论：{'、'.join(str(p['text']) for p in group.premises)}",
            "claim": _synthesized_claim(group),
            "domain_id": primary,
            "classification": {
                "primary_domain_id": primary,
                "related_domain_ids": sorted(set(domains) - {primary}),
                "record_type": record_types.pop() if len(record_types) == 1 else "knowledge",
                "explanation": "沿用依据条目的主领域，审核前仅作为建议。",
            },
            "premises": list(group.premises),
            "excerpt": source_text,
            "evidence": [
                {
                    "type": "formal_knowledge",
                    "entry_id": item["entry_id"],
                    "version": item["version"],
                    "text": f"正式知识「{item['title']}」：{item['claim']}",
                }
                for item in basis
            ],
            "basis": basis,
            "origin": SYSTEM_SYNTHESIS,
        }
        # A basis domain retired since approval makes the draft invalid; skip
        # that group without undoing the rest of the run.
        try:
            with session.begin_nested():
                source = self.repository.persist_source(
                    session, owner, source_text, f"reflection:{key}:source"
                )
                draft = self.repository.create_draft(
                    session, owner, str(source["id"]), payload, f"reflection:{key}:draft"
                )
        except ValueError:
            return None
        return str(draft["id"])


def _formal_knowledge(session: Session, owner: str) -> list[dict[str, Any]]:
    """Formal conclusions that may serve answers now: reflection's only input."""
    rows = session.execute(
        text(
            """
            SELECT e.id, e.approved_version, v.payload_json
            FROM conclusion_entries e
            JOIN conclusion_versions v ON v.entry_id = e.id AND v.version = e.approved_version
            WHERE e.owner_user_id = :o AND e.status = 'formal'
              AND e.current_version = e.approved_version
              AND NOT EXISTS (
                SELECT 1 FROM conclusion_applicability a
                WHERE a.entry_id = e.id AND a.version = e.approved_version
                  AND a.state = 'suspended')
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_objects ko
                WHERE ko.id = e.knowledge_id
                  AND ko.lifecycle_status IN ('soft_deleted', 'privacy_erased'))
              AND NOT EXISTS (
                SELECT 1 FROM conclusion_relations r
                JOIN conclusion_entries le ON le.id = r.left_id
                JOIN conclusion_entries re ON re.id = r.right_id
                WHERE r.owner_user_id = e.owner_user_id AND r.status = 'approved'
                  AND r.kind = 'conflict' AND (r.left_id = e.id OR r.right_id = e.id)
                  AND le.status = 'formal' AND re.status = 'formal')
            ORDER BY e.created_at, e.rowid
            """
        ),
        {"o": owner},
    ).mappings()
    return [
        {**dict(row), "payload": cast(dict[str, Any], json.loads(str(row["payload_json"])))}
        for row in rows
    ]


def _premise_key(premise: object) -> str | None:
    if not isinstance(premise, dict):
        return None
    key = normalize_claim(str(premise.get("text", ""))).lower()
    return key or None


def _groups(entries: list[dict[str, Any]]) -> list[_Group]:
    """Formal conclusions sharing a premise, one group per distinct set of members."""
    by_premise: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        for premise in entry["payload"].get("premises", []):
            key = _premise_key(premise)
            if key is not None and entry not in by_premise.setdefault(key, []):
                by_premise[key].append(entry)
    shared: dict[tuple[str, ...], list[str]] = {}
    members_of: dict[tuple[str, ...], tuple[dict[str, Any], ...]] = {}
    for premise_key, members in by_premise.items():
        collapsed = _collapse_syntheses(members)
        if len(collapsed) < 2 or not _synthesizable(collapsed):
            continue
        ids = tuple(str(member["id"]) for member in collapsed)
        shared.setdefault(ids, []).append(premise_key)
        members_of[ids] = tuple(collapsed)
    return [
        _Group(members_of[ids], tuple(_shared_premise(members_of[ids], k) for k in premise_keys))
        for ids, premise_keys in shared.items()
    ]


def _collapse_syntheses(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An approved synthesis in the group stands for the basis it was drawn from."""
    covered = {
        entry_id
        for member in members
        if member["payload"].get("origin") == SYSTEM_SYNTHESIS
        for entry_id in synthesis_basis_ids(member["payload"])
    }
    return [member for member in members if str(member["id"]) not in covered]


def _synthesizable(members: list[dict[str, Any]]) -> bool:
    """Duplicates, revisions and conflicts are consolidation's to suggest, not synthesis."""
    return not any(
        relation_kind(newer["payload"], older["payload"]) in CONSOLIDATION_KINDS
        for index, newer in enumerate(members)
        for older in members[:index]
    )


def _shared_premise(members: tuple[dict[str, Any], ...], key: str) -> dict[str, Any]:
    """The premise as the members state it; confirmed only if every member confirmed it."""
    stated = [
        premise
        for member in members
        for premise in member["payload"].get("premises", [])
        if _premise_key(premise) == key
    ]
    return {
        "text": str(stated[0].get("text", "")).strip(),
        "confirmed": all(premise.get("confirmed") is True for premise in stated),
    }


def _synthesized_claim(group: _Group) -> str:
    shared = {_premise_key(premise) for premise in group.premises}
    parts = []
    for member in group.members:
        claim = normalize_claim(str(member["payload"].get("claim", "")))
        extra = [
            str(premise.get("text", "")).strip()
            for premise in member["payload"].get("premises", [])
            if _premise_key(premise) not in shared and _premise_key(premise) is not None
        ]
        parts.append(f"{claim}（另需{'、'.join(extra)}）" if extra else claim)
    condition = "」「".join(str(premise["text"]) for premise in group.premises)
    return f"在「{condition}」的前提下，以下结论共同成立：{'；'.join(parts)}。"
