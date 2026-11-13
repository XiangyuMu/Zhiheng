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

from zhiheng.classification.domain_keywords import DomainMatch, match_primary_domains
from zhiheng.classification.taxonomy import PRIMARY_DOMAINS
from zhiheng.core.ids import sha256_json

_PRIMARY_DOMAIN_NAMES = {domain.id: domain.name for domain in PRIMARY_DOMAINS}
QUESTION_DOMAIN_METHOD = "keyword-rule-v2"

PersonalizationReason = Literal["domain_excluded", "domain_not_excluded", "domain_undetermined"]


@dataclass(frozen=True)
class PersonalizationDecision:
    """Which primary domains a question was judged to belong to, and whether
    its answer may read personal memory. ``question_domain_id`` is the domain
    that decided: the first excluded one, otherwise the first matched. A
    question whose domain cannot be judged belongs to no excluded domain."""

    question_domain_id: str | None
    matched_keywords: tuple[str, ...]
    applied: bool
    reason: PersonalizationReason
    method: str = QUESTION_DOMAIN_METHOD
    matched_domains: tuple[DomainMatch, ...] = ()

    @property
    def question_domain_name(self) -> str | None:
        return primary_domain_name(self.question_domain_id)


def ordered_primary_domains() -> list[dict[str, str]]:
    """The fourteen primary domains as id and name, in catalog order."""
    return [
        {"id": domain.id, "name": domain.name}
        for domain in sorted(PRIMARY_DOMAINS, key=lambda item: item.sort_order)
    ]


def primary_domain_name(domain_id: str | None) -> str | None:
    return _PRIMARY_DOMAIN_NAMES.get(domain_id or "")


class PersonalizationSettings:
    def excluded_domain_ids(self, session: Session) -> frozenset[str]:
        rows = session.execute(text("SELECT domain_id FROM personalization_excluded_domains"))
        return frozenset(str(row[0]) for row in rows)

    def replace_excluded_domain_ids(self, session: Session, domain_ids: Iterable[str]) -> None:
        wanted = frozenset(domain_ids)
        self.validate_domain_ids(wanted)
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

    def validate_domain_ids(self, domain_ids: Iterable[str]) -> None:
        unknown = frozenset(domain_ids) - _PRIMARY_DOMAIN_NAMES.keys()
        if unknown:
            raise ValueError(f"not an active primary domain: {', '.join(sorted(unknown))}")

    def decide(self, session: Session, query: str) -> PersonalizationDecision:
        matches = tuple(match_primary_domains(query))
        if not matches:
            return PersonalizationDecision(None, (), applied=True, reason="domain_undetermined")
        excluded = self.excluded_domain_ids(session)
        for domain_id, keywords in matches:
            if domain_id in excluded:
                return PersonalizationDecision(
                    domain_id, keywords, False, "domain_excluded", matched_domains=matches
                )
        domain_id, keywords = matches[0]
        return PersonalizationDecision(
            domain_id, keywords, True, "domain_not_excluded", matched_domains=matches
        )

    def payload(self, session: Session) -> dict[str, Any]:
        excluded = self.excluded_domain_ids(session)
        ordered = sorted(excluded)
        return {
            "excluded_domain_ids": ordered,
            "domains": [
                {**domain, "excluded": domain["id"] in excluded}
                for domain in ordered_primary_domains()
            ],
            "etag": personalization_etag(ordered),
        }


def personalization_etag(excluded_domain_ids: Iterable[str]) -> str:
    return f"personalization:{sha256_json({'excluded': sorted(excluded_domain_ids)})}"
