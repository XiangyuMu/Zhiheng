from __future__ import annotations

from pathlib import Path

from zhiheng.conclusions.extraction import ExtractedConclusion
from zhiheng.evaluation.issue29_conclusion_extraction import (
    MIN_DIALOGUES,
    evaluate_issue29_conclusion_extraction,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "conclusions"
    / "issue29_synthetic_dialogues.json"
)


def test_issue29_fixed_set_meets_all_extraction_gates() -> None:
    result = evaluate_issue29_conclusion_extraction(FIXTURE)

    assert result.metrics.dialogue_count >= MIN_DIALOGUES
    assert result.metrics.conclusion_recall >= 0.90
    assert result.metrics.premise_recall >= 0.95
    assert result.metrics.offset_validity == 1.0
    assert result.metrics.negative_false_extractions == 0
    assert result.passed


def test_issue29_evaluation_invokes_the_supplied_extractor() -> None:
    calls: list[tuple[str, str]] = []

    def spy(*, query: str, answer: str) -> list[ExtractedConclusion]:
        calls.append((query, answer))
        return []

    result = evaluate_issue29_conclusion_extraction(FIXTURE, extractor=spy)

    assert len(calls) == result.metrics.dialogue_count
    assert result.metrics.matched_claim_count == 0
