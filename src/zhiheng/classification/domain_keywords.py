"""Deterministic primary-domain keyword rules shared by suggestions and answers."""

from __future__ import annotations

DOMAIN_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
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


def match_primary_domains(value: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return each domain whose keywords occur in the text, in rule order."""
    folded = value.casefold()
    matches: list[tuple[str, tuple[str, ...]]] = []
    for keywords, domain_id in DOMAIN_KEYWORDS:
        found = tuple(keyword for keyword in keywords if keyword.casefold() in folded)
        if found:
            matches.append((domain_id, found))
    return matches
