"""Deterministic primary-domain keyword rules shared by suggestions and answers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# A matched domain id and the keywords that counted for it.
DomainMatch = tuple[str, tuple[str, ...]]


@dataclass(frozen=True)
class DomainRule:
    domain_id: str
    keywords: tuple[str, ...]
    # Words common across many topics. When judging a question they count
    # only if no rule's specific keywords match.
    general_keywords: tuple[str, ...] = ()


DOMAIN_RULES: tuple[DomainRule, ...] = (
    DomainRule("mathematics_formal_sciences", ("数学", "概率", "统计"), ("逻辑",)),
    DomainRule("natural_sciences", ("物理", "化学", "生物", "地球", "宇宙")),
    DomainRule("computing_engineering", ("代码", "软件", "ai", "计算机")),
    DomainRule("medicine_health", ("健康", "睡眠", "运动", "营养")),
    DomainRule("psychology_cognition", ("心理", "情绪", "动机"), ("认知", "行为")),
    DomainRule("society_politics_law", ("社会", "政治", "法律", "政策", "制度")),
    DomainRule("history_philosophy_religion", ("历史", "哲学", "宗教", "伦理")),
    DomainRule("language_literature_arts", ("语言", "文学", "艺术", "音乐", "创作")),
    DomainRule("education_learning", ("教育",), ("学习", "记忆", "练习")),
    DomainRule("career_work_practice", ("工作", "职业", "求职", "团队")),
    DomainRule("relationships_communication", ("家庭", "沟通", "冲突"), ("关系",)),
    DomainRule("economics_finance_business", ("投资", "金融", "商业"), ("钱",)),
    DomainRule("lifestyle_daily_life", ("饮食", "出行", "居家"), ("生活",)),
    DomainRule("sports_games_leisure", ("体育", "游戏", "休闲", "娱乐")),
)

# Everyday phrases that contain a keyword without being about its domain.
IGNORED_PHRASES: tuple[str, ...] = (
    "没关系",
    "没有关系",
    "有关系",
    "关系到",
    "怎么工作",
    "如何工作",
    "工作原理",
)


def _occurs(keyword: str, folded: str) -> bool:
    if keyword.isascii():
        # English keywords match whole words, so "ai" does not match "email".
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", folded) is not None
    return keyword in folded


def match_primary_domains(value: str, *, prefer_specific: bool = True) -> list[DomainMatch]:
    """Return each domain whose keywords occur in the text, in rule order,
    with the keywords that counted. With ``prefer_specific``, general keywords
    count only when no specific keyword matches."""
    folded = value.casefold()
    for phrase in IGNORED_PHRASES:
        folded = folded.replace(phrase, " ")

    def matches(keywords_of: Callable[[DomainRule], tuple[str, ...]]) -> list[DomainMatch]:
        result = []
        for rule in DOMAIN_RULES:
            found = tuple(keyword for keyword in keywords_of(rule) if _occurs(keyword, folded))
            if found:
                result.append((rule.domain_id, found))
        return result

    if not prefer_specific:
        return matches(lambda rule: rule.keywords + rule.general_keywords)
    return matches(lambda rule: rule.keywords) or matches(lambda rule: rule.general_keywords)
