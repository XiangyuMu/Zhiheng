from __future__ import annotations

import pytest

from zhiheng.knowledge.pdf_text_quality import (
    BBox,
    OCRTrigger,
    TextLayerRegion,
    classify_text_layer,
)


def region(
    key: str,
    text: str,
    bbox: tuple[float, float, float, float],
    confidence: float | None = None,
) -> TextLayerRegion:
    return TextLayerRegion(key, text, BBox(*bbox), confidence)


def test_healthy_text_layer_is_preserved_without_ocr() -> None:
    decision = classify_text_layer(
        [region("heading", "Heading", (0, 0, 100, 20), confidence=0.99)]
    )[0]

    assert decision.use_ocr is False
    assert decision.trigger is None
    assert decision.preserved_text == "Heading"


@pytest.mark.parametrize(
    ("text", "confidence", "trigger"),
    [
        ("", 0.99, OCRTrigger.MISSING),
        ("   ", 0.99, OCRTrigger.MISSING),
        ("bad\ufffdtext", 0.99, OCRTrigger.GARBLED),
        ("clear text", 0.40, OCRTrigger.LOW_CONFIDENCE),
    ],
)
def test_only_unhealthy_regions_are_selected(
    text: str, confidence: float, trigger: OCRTrigger
) -> None:
    decisions = classify_text_layer(
        [
            region("healthy", "Keep me", (0, 0, 100, 20), confidence=0.99),
            region("needs-ocr", text, (0, 30, 100, 50), confidence=confidence),
        ]
    )

    assert decisions[0].use_ocr is False
    assert decisions[1].use_ocr is True
    assert decisions[1].trigger is trigger
    assert decisions[1].preserved_text == text


def test_overlapping_regions_are_ocr_candidates_but_bbox_is_local() -> None:
    decisions = classify_text_layer(
        [
            region("first", "one", (0, 0, 100, 20), confidence=0.99),
            region("second", "two", (10, 0, 90, 20), confidence=0.99),
        ]
    )

    assert [decision.trigger for decision in decisions] == [
        OCRTrigger.OVERLAP,
        OCRTrigger.OVERLAP,
    ]
    assert decisions[0].bbox == BBox(0, 0, 100, 20)
    assert decisions[1].bbox == BBox(10, 0, 90, 20)


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="threshold"):
        classify_text_layer([], low_confidence_threshold=1.1)
