"""Labelled questions the primary-domain keyword rules must judge correctly."""

from __future__ import annotations

import pytest

from zhiheng.classification.domain_keywords import match_primary_domains

LABELLED_QUESTIONS: dict[str, list[str]] = {
    # One clear domain.
    "睡眠不足有什么影响？": ["medicine_health"],
    "统计学里的概率是什么": ["mathematics_formal_sciences"],
    "我现在写软件应该注意什么": ["computing_engineering"],
    "AI模型会取代程序员吗": ["computing_engineering"],
    "家庭里的沟通冲突怎么处理": ["relationships_communication"],
    # A question can belong to several domains; every one is reported in rule order.
    "工作压力影响睡眠怎么办": ["medicine_health", "career_work_practice"],
    # English keywords match whole words only.
    "How do I maintain my email inbox?": [],
    "Will AI replace translators?": ["computing_engineering"],
    # Everyday phrases that only look like keywords.
    "没关系，我们换个话题": [],
    "这个程序是怎么工作的": [],
    # General words count only when nothing more specific matches.
    "学习投资的方法": ["economics_finance_business"],
    "生活中如何管理情绪": ["psychology_cognition"],
    "哲学和宗教的关系": ["history_philosophy_religion"],
    "我和父母的关系很紧张": ["relationships_communication"],
    "怎么存钱": ["economics_finance_business"],
    # No domain can be judged.
    "中文 全文 检索 正式 视图": [],
}


@pytest.mark.parametrize(("question", "expected"), LABELLED_QUESTIONS.items())
def test_labelled_question_is_judged_in_its_domains(question: str, expected: list[str]) -> None:
    assert [domain_id for domain_id, _ in match_primary_domains(question)] == expected


def test_matches_report_only_the_keywords_that_counted() -> None:
    assert match_primary_domains("工作压力影响睡眠，生活一团糟") == [
        ("medicine_health", ("睡眠",)),
        ("career_work_practice", ("工作",)),
    ]
    assert match_primary_domains("怎么存钱") == [("economics_finance_business", ("钱",))]
