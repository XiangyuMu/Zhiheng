from __future__ import annotations

import pytest

from zhiheng.knowledge.pdf_quality import (
    BBox,
    OCRTrigger,
    TextLayerRegion,
    classify_text_layer,
)


def _region(
    key: str,
    text: str,
    bbox: tuple[float, float, float, float],
    confidence: float | None = None,
) -> TextLayerRegion:
    return TextLayerRegion(key, text, BBox(*bbox), confidence)


@pytest.mark.parametrize(
    ("text", "confidence", "trigger"),
    [
        ("", 0.99, OCRTrigger.MISSING),
        ("   ", 0.99, OCRTrigger.MISSING),
        ("broken\ufffdtext", 0.99, OCRTrigger.GARBLED),
        ("clear text", 0.40, OCRTrigger.LOW_CONFIDENCE),
    ],
)
def test_region_quality_has_explicit_ocr_triggers(
    text: str, confidence: float, trigger: OCRTrigger
) -> None:
    decision = classify_text_layer([_region("r", text, (0, 0, 100, 20), confidence)])[0]

    assert decision.use_ocr is True
    assert decision.trigger is trigger
    assert decision.preserved_text == text


def test_healthy_region_is_preserved_and_not_sent_to_ocr() -> None:
    decision = classify_text_layer(
        [_region("healthy", "正文", (10, 20, 100, 40), confidence=0.99)]
    )[0]

    assert decision.use_ocr is False
    assert decision.trigger is None
    assert decision.preserved_text == "正文"
    assert decision.bbox == BBox(10, 20, 100, 40)


def test_overlap_is_region_local_and_does_not_replace_other_text() -> None:
    decisions = classify_text_layer(
        [
            _region("first", "one", (0, 0, 100, 20), confidence=0.99),
            _region("second", "two", (10, 0, 90, 20), confidence=0.99),
        ]
    )

    assert [item.trigger for item in decisions] == [
        OCRTrigger.OVERLAP,
        OCRTrigger.OVERLAP,
    ]
    assert [item.preserved_text for item in decisions] == ["one", "two"]


def test_invalid_quality_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError, match="threshold"):
        classify_text_layer([], low_confidence_threshold=1.1)
    with pytest.raises(ValueError, match="threshold"):
        classify_text_layer([], overlap_threshold=-0.1)
