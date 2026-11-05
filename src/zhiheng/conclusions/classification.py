"""Deterministic classification suggestions for conclusion review."""

from __future__ import annotations

from typing import Any

from zhiheng.classification.domain_keywords import match_primary_domains
from zhiheng.classification.taxonomy import LEGACY_DOMAIN_MAPPING, PRIMARY_DOMAINS

_PRIMARY_DOMAIN_IDS = frozenset(domain.id for domain in PRIMARY_DOMAINS)
_LEGACY_DOMAIN_IDS = frozenset(LEGACY_DOMAIN_MAPPING)


def _is_known_domain(domain_id: str) -> bool:
    # Older conclusion records use dotted IDs (for example technology.ai).
    # Keep those resolvable while all new suggestions use the v2 catalog.
    return domain_id in _PRIMARY_DOMAIN_IDS or domain_id in _LEGACY_DOMAIN_IDS or "." in domain_id


def suggest_classification(*, title: str, claim: str, domain_id: str) -> dict[str, Any]:
    text = f"{title} {claim}".casefold()
    matched = [
        candidate for candidate, _keywords in match_primary_domains(text, prefer_specific=False)
    ]
    primary = domain_id if domain_id in matched else (matched[0] if matched else domain_id)
    related = set(matched)
    related.discard(primary)
    personal = any(marker in text for marker in ("我", "我的", "经历", "经验", "决定", "反思"))
    return {
        "primary_domain_id": primary,
        "related_domain_ids": sorted(related),
        "record_type": "personal_archive_experience" if personal else "knowledge",
        "source": "rule",
        "explanation": "根据结论标题和内容关键词生成，审核前仅作为建议。",
    }


def normalize_classification(
    value: dict[str, Any] | None,
    *,
    fallback_domain_id: str,
    title: str,
    claim: str,
    allowed_domain_ids: set[str] | None = None,
    inactive_domain_ids: set[str] | None = None,
    allowed_record_types: set[str] | None = None,
) -> dict[str, Any]:
    suggestion = suggest_classification(title=title, claim=claim, domain_id=fallback_domain_id)
    user_value = value is not None
    if not value:
        value = suggestion
    primary = str(value.get("primary_domain_id") or fallback_domain_id)
    primary_is_catalog_domain = allowed_domain_ids is not None and primary in allowed_domain_ids
    if (
        not (_is_known_domain(primary) or primary_is_catalog_domain)
        or (inactive_domain_ids is not None and primary in inactive_domain_ids)
        or (
            allowed_domain_ids is not None
            and primary not in allowed_domain_ids
            and "." not in primary
        )
    ):
        raise ValueError("unsupported conclusion primary domain")
    related = sorted(
        {
            str(item)
            for item in value.get("related_domain_ids", suggestion["related_domain_ids"])
            if str(item) and str(item) != primary
        }
    )
    if any(
        not (
            _is_known_domain(item)
            or (allowed_domain_ids is not None and item in allowed_domain_ids)
        )
        or (inactive_domain_ids is not None and item in inactive_domain_ids)
        or (allowed_domain_ids is not None and item not in allowed_domain_ids and "." not in item)
        for item in related
    ):
        raise ValueError("unsupported conclusion related domain")
    record_type = str(value.get("record_type") or suggestion["record_type"])
    if record_type not in {"knowledge", "personal_archive_experience"} or (
        allowed_record_types is not None and record_type not in allowed_record_types
    ):
        raise ValueError("unsupported conclusion record type")
    return {
        "primary_domain_id": primary,
        "related_domain_ids": related,
        "record_type": record_type,
        "source": "user" if user_value else suggestion["source"],
        "explanation": str(value.get("explanation") or suggestion["explanation"]),
    }
