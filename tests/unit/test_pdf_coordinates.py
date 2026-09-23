from __future__ import annotations

import pytest

from zhiheng.knowledge.pdf_coordinates import (
    TRANSFORM_VERSION,
    convert_bbox,
    convert_point,
    displayed_page_size,
)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (20.0, 70.0)),
        (90, (30.0, 20.0)),
        (180, (180.0, 30.0)),
        (270, (70.0, 180.0)),
    ],
)
def test_cropbox_point_conversion_uses_top_left_origin(
    rotation: int, expected: tuple[float, float]
) -> None:
    assert convert_point((30, 50), (10, 20, 210, 120), rotation=rotation) == expected


def test_bbox_transforms_all_corners_and_rounds_half_up() -> None:
    assert convert_bbox(
        (10.0005, 20.0005, 11.0015, 21.0015),
        (0, 0, 100, 100),
    ) == (10.001, 78.999, 11.002, 80.0)


def test_user_unit_and_rotated_page_size_are_applied() -> None:
    assert displayed_page_size((10, 20, 210, 120), rotation=90, user_unit=2) == (
        200.0,
        400.0,
    )
    assert convert_point((30, 50), (10, 20, 210, 120), rotation=90, user_unit=2) == (60.0, 40.0)


def test_transform_version_is_stable() -> None:
    assert TRANSFORM_VERSION == "pdf-crop-rotate-v1"


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ((0, 0, 1), "point"),
        ((0, 0, 1), "crop_box"),
        ((1, 1, 0, 2), "bbox"),
    ],
)
def test_invalid_geometry_is_rejected(value: tuple[float, ...], name: str) -> None:
    with pytest.raises(ValueError, match=name):
        if name == "point":
            convert_point(value, (0, 0, 1, 1))
        elif name == "crop_box":
            convert_point((0, 0), value)
        else:
            convert_bbox(value, (0, 0, 1, 1))


def test_invalid_rotation_and_user_unit_are_rejected() -> None:
    with pytest.raises(ValueError, match="rotation"):
        convert_point((0, 0), (0, 0, 1, 1), rotation=45)
    with pytest.raises(ValueError, match="user_unit"):
        convert_point((0, 0), (0, 0, 1, 1), user_unit=0)
