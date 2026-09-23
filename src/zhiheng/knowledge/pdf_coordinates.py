"""Canonical PDF coordinate conversion for parser evidence.

PDF backends commonly report coordinates in the original PDF user space,
whose origin is at the lower-left of the CropBox.  Zhiheng stores evidence
in displayed PDF points with a top-left origin.  The conversion here is
deliberately dependency-free and deterministic so parser adapters can share
the same geometry contract.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

TRANSFORM_VERSION = "pdf-crop-rotate-v1"
_ROUNDING_QUANTUM = Decimal("0.001")
_VALID_ROTATIONS = frozenset({0, 90, 180, 270})


def convert_point(
    point: Sequence[float],
    crop_box: Sequence[float],
    *,
    rotation: int = 0,
    user_unit: float = 1.0,
) -> tuple[float, float]:
    """Convert one PDF user-space point to displayed top-left coordinates."""
    x, y = _pair(point, "point")
    left, bottom, right, top = _rect(crop_box, "crop_box")
    unit = _positive_finite(user_unit, "user_unit")
    _rotation(rotation)

    x = (x - left) * unit
    y = (y - bottom) * unit
    width = (right - left) * unit
    height = (top - bottom) * unit

    if rotation == 0:
        converted = (x, height - y)
    elif rotation == 90:
        converted = (y, x)
    elif rotation == 180:
        converted = (width - x, y)
    else:
        converted = (height - y, width - x)
    return _rounded_pair(converted)


def convert_bbox(
    bbox: Sequence[float],
    crop_box: Sequence[float],
    *,
    rotation: int = 0,
    user_unit: float = 1.0,
) -> tuple[float, float, float, float]:
    """Convert a rectangle by transforming all corners and taking its envelope."""
    x0, y0, x1, y1 = _rect(bbox, "bbox")
    corners = (
        convert_point((x0, y0), crop_box, rotation=rotation, user_unit=user_unit),
        convert_point((x0, y1), crop_box, rotation=rotation, user_unit=user_unit),
        convert_point((x1, y0), crop_box, rotation=rotation, user_unit=user_unit),
        convert_point((x1, y1), crop_box, rotation=rotation, user_unit=user_unit),
    )
    xs = [corner[0] for corner in corners]
    ys = [corner[1] for corner in corners]
    return _rounded_rect((min(xs), min(ys), max(xs), max(ys)))


def displayed_page_size(
    crop_box: Sequence[float],
    *,
    rotation: int = 0,
    user_unit: float = 1.0,
) -> tuple[float, float]:
    """Return displayed page width and height in PDF points."""
    left, bottom, right, top = _rect(crop_box, "crop_box")
    unit = _positive_finite(user_unit, "user_unit")
    _rotation(rotation)
    width = (right - left) * unit
    height = (top - bottom) * unit
    if rotation in {90, 270}:
        width, height = height, width
    return _rounded_pair((width, height))


def _rotation(rotation: int) -> None:
    if isinstance(rotation, bool) or rotation not in _VALID_ROTATIONS:
        raise ValueError("rotation must be one of 0, 90, 180, or 270 degrees")


def _pair(value: Sequence[float], name: str) -> tuple[float, float]:
    if len(value) != 2:
        raise ValueError(f"{name} must contain exactly two coordinates")
    first, second = (float(item) for item in value)
    if not math.isfinite(first) or not math.isfinite(second):
        raise ValueError(f"{name} coordinates must be finite")
    return first, second


def _rect(value: Sequence[float], name: str) -> tuple[float, float, float, float]:
    if len(value) != 4:
        raise ValueError(f"{name} must contain exactly four coordinates")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        raise ValueError(f"{name} coordinates must be finite")
    if x1 < x0 or y1 < y0:
        raise ValueError(f"{name} must have non-negative extents")
    return x0, y0, x1, y1


def _positive_finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return value


def _rounded_pair(value: tuple[float, float]) -> tuple[float, float]:
    return tuple(_round_coordinate(item) for item in value)  # type: ignore[return-value]


def _rounded_rect(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(_round_coordinate(item) for item in value)  # type: ignore[return-value]


def _round_coordinate(value: float) -> float:
    rounded = Decimal(str(value)).quantize(_ROUNDING_QUANTUM, rounding=ROUND_HALF_UP)
    return 0.0 if rounded == 0 else float(rounded)


__all__ = [
    "TRANSFORM_VERSION",
    "convert_bbox",
    "convert_point",
    "displayed_page_size",
]
