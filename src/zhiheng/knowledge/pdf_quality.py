"""Deterministic text-layer quality decisions for PDF parsing.

The DeepDoc worker owns OCR execution. This module only classifies extracted
regions and returns bounded OCR work items. Healthy text is preserved and
only regions with an explicit quality failure are sent to OCR.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class BBox:
    """A finite, non-negative rectangle in the backend's raw coordinate space."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("bbox coordinates must be finite")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox must have non-negative extents")

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    def overlap_ratio(self, other: BBox) -> float:
        """Return intersection area divided by the smaller positive area."""
        intersection = max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0)) * max(
            0.0, min(self.y1, other.y1) - max(self.y0, other.y0)
        )
        smaller = min(self.area, other.area)
        return intersection / smaller if smaller > 0 else 0.0


@dataclass(frozen=True)
class TextLayerRegion:
    """A region emitted by text extraction/layout detection."""

    key: str
    text: str
    bbox: BBox
    confidence: float | None = None


class OCRTrigger(StrEnum):
    MISSING = "missing"
    GARBLED = "garbled"
    OVERLAP = "overlap"
    LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class OCRDecision:
    """The bounded OCR action for one text-layer region."""

    key: str
    bbox: BBox
    use_ocr: bool
    trigger: OCRTrigger | None
    preserved_text: str
    confidence: float | None


def classify_text_layer(
    regions: list[TextLayerRegion],
    *,
    low_confidence_threshold: float = 0.75,
    overlap_threshold: float = 0.20,
) -> list[OCRDecision]:
    """Classify regions without invoking OCR.

    A region is OCR-eligible when its text is absent/blank, visibly corrupted,
    overlaps another extracted region, or has low confidence. Regions are
    evaluated in input order and the first matching trigger is retained in the
    deterministic priority order: missing, garbled, low-confidence, overlap.
    The original text and local bbox are always retained for provenance.
    """
    if not 0.0 <= low_confidence_threshold <= 1.0:
        raise ValueError("low_confidence_threshold must be between 0 and 1")
    if not 0.0 <= overlap_threshold <= 1.0:
        raise ValueError("overlap_threshold must be between 0 and 1")

    decisions: list[OCRDecision] = []
    for index, region in enumerate(regions):
        trigger = _intrinsic_trigger(region, low_confidence_threshold)
        if trigger is None and any(
            index != other_index and region.bbox.overlap_ratio(other.bbox) >= overlap_threshold
            for other_index, other in enumerate(regions)
        ):
            trigger = OCRTrigger.OVERLAP
        decisions.append(
            OCRDecision(
                key=region.key,
                bbox=region.bbox,
                use_ocr=trigger is not None,
                trigger=trigger,
                preserved_text=region.text,
                confidence=region.confidence,
            )
        )
    return decisions


def _intrinsic_trigger(
    region: TextLayerRegion, low_confidence_threshold: float
) -> OCRTrigger | None:
    if not region.text.strip():
        return OCRTrigger.MISSING
    if _looks_garbled(region.text):
        return OCRTrigger.GARBLED
    if region.confidence is not None and region.confidence < low_confidence_threshold:
        return OCRTrigger.LOW_CONFIDENCE
    return None


def _looks_garbled(text: str) -> bool:
    """Detect common extraction corruption without judging legitimate scripts."""
    if "\ufffd" in text:
        return True
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return False
    control_count = sum(
        unicodedata.category(character).startswith("C")
        and character not in {"\u200b", "\u200c", "\u200d"}
        for character in visible
    )
    if control_count / len(visible) >= 0.20:
        return True
    private_use_count = sum(unicodedata.category(character) == "Co" for character in visible)
    return len(visible) >= 3 and private_use_count / len(visible) >= 0.50


__all__ = [
    "BBox",
    "OCRDecision",
    "OCRTrigger",
    "TextLayerRegion",
    "classify_text_layer",
]
