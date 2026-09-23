from __future__ import annotations

import pytest

from zhiheng.models import ImagePart, ModelContentPart, ModelRequest, TextPart


def test_text_only_model_request_remains_compatible() -> None:
    request = ModelRequest(
        task_id="task-1",
        provider_id="provider-1",
        model_id="model-1",
        payload="plain text",
    )

    assert request.payload == "plain text"
    assert request.parts == ()


def test_model_request_accepts_typed_text_and_image_parts() -> None:
    image = ImagePart(
        artifact_uri="artifact://images/one",
        sha256="a" * 64,
        media_type="image/png",
    )
    request = ModelRequest(
        task_id="task-1",
        provider_id="provider-1",
        model_id="model-1",
        payload="describe this",
        parts=(TextPart("Please inspect the image."), image),
    )

    assert isinstance(request.parts[0], TextPart)
    assert isinstance(request.parts[1], ImagePart)
    assert ModelContentPart == TextPart | ImagePart


@pytest.mark.parametrize(
    ("artifact_uri", "sha256", "media_type"),
    [
        ("images/one", "a" * 64, "image/png"),
        ("artifact://images/one", "bad", "image/png"),
        ("artifact://images/one", "a" * 64, "application/pdf"),
    ],
)
def test_image_part_rejects_invalid_uri_hash_or_media_type(
    artifact_uri: str,
    sha256: str,
    media_type: str,
) -> None:
    with pytest.raises(ValueError):
        ImagePart(artifact_uri=artifact_uri, sha256=sha256, media_type=media_type)
