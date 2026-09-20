"""Deterministic classification suggestions for conclusion review."""

from __future__ import annotations

from typing import Any

from zhiheng.classification.taxonomy import LEGACY_DOMAIN_MAPPING, PRIMARY_DOMAINS

_DOMAIN_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("数学", "逻辑", "概率", "统计"), "mathematics_formal_sciences"),
    (("物理", "化学", "生物", "地球", "宇宙"), "natural_sciences"),
    (("代码", "软件", "ai", "计算机"), "computing_engineering"),
    (("健康", "睡眠", "运动", "营养"), "medicine_health"),
    (("心理", "情绪", "动机", "认知", "行为"), "psychology_cognition"),
    (("社会", "政治", "法律", "政策", "制度"), "society_politics_law"),
    (("历史", "哲学", "宗教", "伦理"), "history_philosophy_religion"),
    (("语言", "文学", "艺术", "音乐", "创作"), "language_literature_arts"),
    (("学习", "记忆", "练习", "教育"), "education_learning"),
    (("工作", "职业", "求职", "团队"), "career_work_practice"),
    (("家庭", "关系", "沟通", "冲突"), "relationships_communication"),
    (("投资", "金融", "钱", "商业"), "economics_finance_business"),
    (("生活", "饮食", "出行", "居家"), "lifestyle_daily_life"),
    (("体育", "游戏", "休闲", "娱乐"), "sports_games_leisure"),
)
_PRIMARY_DOMAIN_IDS = frozenset(domain.id for domain in PRIMARY_DOMAINS)
_LEGACY_DOMAIN_IDS = frozenset(LEGACY_DOMAIN_MAPPING)


def _is_known_domain(domain_id: str) -> bool:
    # Older conclusion records use dotted IDs (for example technology.ai).
    # Keep those resolvable while all new suggestions use the v2 catalog.
    return domain_id in _PRIMARY_DOMAIN_IDS or domain_id in _LEGACY_DOMAIN_IDS or "." in domain_id


def suggest_classification(*, title: str, claim: str, domain_id: str) -> dict[str, Any]:
    text = f"{title} {claim}".casefold()
    matched = [
        candidate
        for keywords, candidate in _DOMAIN_KEYWORDS
        if any(keyword.casefold() in text for keyword in keywords)
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
    value: dict[str, Any] | None, *, fallback_domain_id: str, title: str, claim: str
) -> dict[str, Any]:
    suggestion = suggest_classification(title=title, claim=claim, domain_id=fallback_domain_id)
    if not value:
        return suggestion
    primary = str(value.get("primary_domain_id") or fallback_domain_id)
    if not _is_known_domain(primary):
        raise ValueError("unsupported conclusion primary domain")
    related = sorted(
        {
            str(item)
            for item in value.get("related_domain_ids", suggestion["related_domain_ids"])
            if str(item) and str(item) != primary
        }
    )
    if any(not _is_known_domain(item) for item in related):
        raise ValueError("unsupported conclusion related domain")
    record_type = str(value.get("record_type") or suggestion["record_type"])
    if record_type not in {"knowledge", "personal_archive_experience"}:
        raise ValueError("unsupported conclusion record type")
    return {
        "primary_domain_id": primary,
        "related_domain_ids": related,
        "record_type": record_type,
        "source": "user" if value else suggestion["source"],
        "explanation": str(value.get("explanation") or suggestion["explanation"]),
    }
