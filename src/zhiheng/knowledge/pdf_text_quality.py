"""Backward-compatible import path for PDF text-layer quality decisions."""

from zhiheng.knowledge.pdf_quality import (
    BBox,
    OCRDecision,
    OCRTrigger,
    TextLayerRegion,
    classify_text_layer,
)

__all__ = [
    "BBox",
    "OCRDecision",
    "OCRTrigger",
    "TextLayerRegion",
    "classify_text_layer",
]
