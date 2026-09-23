from __future__ import annotations

import pytest

from zhiheng.classification.suggestions import (
    ClassificationNode,
    RuleCandidateSelector,
    parse_model_suggestions,
)


def _nodes() -> list[ClassificationNode]:
    return [
        ClassificationNode("finance", "金融分析", "金融/分析", 2),
        ClassificationNode("software", "软件工程", "技术/软件工程", 2),
    ]


def test_rule_selector_prefers_title_match_and_is_bounded() -> None:
    selected = RuleCandidateSelector(max_candidates=1).select(
        _nodes(), title="金融分析周报", text_content="本周市场变化"
    )
    assert [item.id for item in selected] == ["finance"]


def test_model_output_is_strictly_bound_to_candidates() -> None:
    parsed = parse_model_suggestions(
        '{"suggestions":[{"node_id":"finance","confidence":0.91,"explanation":"标题命中"}]}',
        _nodes(),
    )
    assert parsed[0].node_id == "finance"
    assert parsed[0].confidence == 0.91

    with pytest.raises(ValueError, match="unknown|schema"):
        parse_model_suggestions(
            '{"suggestions":[{"node_id":"attacker","confidence":1,"explanation":"x"}]}',
            _nodes(),
        )


def test_model_output_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="schema mismatch"):
        parse_model_suggestions(
            '{"suggestions":[{"node_id":"finance","confidence":2,"explanation":"x"}]}',
            _nodes(),
        )
