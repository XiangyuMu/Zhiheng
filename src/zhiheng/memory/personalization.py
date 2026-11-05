"""Primary domains whose questions are answered without personal memory (#111).

Excluding a domain only stops answers from reading personal memory. Personal
statements made in those questions are still remembered as usual.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.classification.domain_keywords import match_primary_domains
from zhiheng.classification.taxonomy import PRIMARY_DOMAINS
from zhiheng.core.ids import sha256_json

_PRIMARY_DOMAIN_NAMES = {domain.id: domain.name for domain in PRIMARY_DOMAINS}
QUESTION_DOMAIN_METHOD = "keyword-rule-v1"

PersonalizationReason = Literal["domain_excluded", "domain_not_excluded", "domain_undetermined"]


@dataclass(frozen=True)
class PersonalizationDecision:
    """Which primary domain a question was judged to belong to, and whether
    its answer may read personal memory. A question whose domain cannot be
    judged is treated as belonging to no excluded domain."""

    question_domain_id: str | None
    matched_keywords: tuple[str, ...]
    applied: bool
    reason: PersonalizationReason
    method: str = QUESTION_DOMAIN_METHOD

    @property
    def question_domain_name(self) -> str | None:
        return _PRIMARY_DOMAIN_NAMES.get(self.question_domain_id or "")


def judge_question_domain(query: str) -> tuple[str | None, tuple[str, ...]]:
    """The question's primary domain is the first rule that matches, as for
    conclusion classification suggestions."""
    matches = match_primary_domains(query)
    if not matches:
        return None, ()
    return matches[0]


class PersonalizationSettings:
    def excluded_domain_ids(self, session: Session) -> frozenset[str]:
        rows = session.execute(text("SELECT domain_id FROM personalization_excluded_domains"))
        return frozenset(str(row[0]) for row in rows)

    def replace_excluded_domain_ids(self, session: Session, domain_ids: Iterable[str]) -> None:
        wanted = frozenset(domain_ids)
        unknown = wanted - _PRIMARY_DOMAIN_NAMES.keys()
        if unknown:
            raise ValueError(f"not an active primary domain: {', '.join(sorted(unknown))}")
        current = self.excluded_domain_ids(session)
        for domain_id in current - wanted:
            session.execute(
                text("DELETE FROM personalization_excluded_domains WHERE domain_id = :id"),
                {"id": domain_id},
            )
        for domain_id in wanted - current:
            session.execute(
                text("INSERT INTO personalization_excluded_domains (domain_id) VALUES (:id)"),
                {"id": domain_id},
            )

    def decide(self, session: Session, query: str) -> PersonalizationDecision:
        domain_id, keywords = judge_question_domain(query)
        if domain_id is None:
            return PersonalizationDecision(None, (), applied=True, reason="domain_undetermined")
        if domain_id in self.excluded_domain_ids(session):
            return PersonalizationDecision(
                domain_id, keywords, applied=False, reason="domain_excluded"
            )
        return PersonalizationDecision(
            domain_id, keywords, applied=True, reason="domain_not_excluded"
        )

    def payload(self, session: Session) -> dict[str, Any]:
        excluded = self.excluded_domain_ids(session)
        ordered = sorted(excluded)
        return {
            "excluded_domain_ids": ordered,
            "domains": [
                {"id": domain.id, "name": domain.name, "excluded": domain.id in excluded}
                for domain in sorted(PRIMARY_DOMAINS, key=lambda item: item.sort_order)
            ],
            "etag": personalization_etag(ordered),
        }


def personalization_etag(excluded_domain_ids: Iterable[str]) -> str:
    return f"personalization:{sha256_json({'excluded': sorted(excluded_domain_ids)})}"
