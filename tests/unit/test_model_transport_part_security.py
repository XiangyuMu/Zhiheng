from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_auth_and_model_gateway import (
    _insert_provider,
    _migrated_session_factory,
)
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import session_scope
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore
from zhiheng.models import ImagePart, ModelGateway, ModelRequest, TextPart
from zhiheng.models._transports import (
    OpenAICompatibleChatTransport,
    TransportResponse,
    TransportRoute,
    _ApprovedImagePart,
    _ApprovedOutboundPayload,
    _ApprovedTextPart,
)
from zhiheng.privacy.gateway import DeterministicPatternAnalyzer, PrivacyPipeline
from zhiheng.secrets import EnvironmentSecretStore

_PNG_BYTES = b"\x89PNG\r\n\x1a\nsynthetic-image-bytes"


@dataclass
class CapturingTransport:
    payloads: list[_ApprovedOutboundPayload]

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        del route
        self.payloads.append(payload)
        return TransportResponse(text="ok", response_hash=sha256_text("ok"))


def test_gateway_redacts_text_parts_and_resolves_images_to_bounded_data_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    object_store = LocalKnowledgeObjectStore(tmp_path / "objects")
    image = object_store.write_binary_artifact(_PNG_BYTES, namespace="images")
    transport = CapturingTransport(payloads=[])
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "external_models_enabled": True,
                "knowledge_object_store_path": str(object_store.root),
            }
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    gateway.complete(
        ModelRequest(
            task_id="task-1",
            provider_id="provider-openai",
            model_id="gpt-test",
            payload="safe prompt",
            parts=(
                TextPart("include owner@example.test safely"),
                ImagePart(
                    artifact_uri=image.uri,
                    sha256=image.sha256,
                    media_type="image/png",
                ),
            ),
        )
    )

    outbound = transport.payloads[0]
    text_part = outbound.parts[0]
    image_part = outbound.parts[1]
    assert isinstance(text_part, _ApprovedTextPart)
    assert isinstance(image_part, _ApprovedImagePart)
    assert text_part.text == "include [REDACTED_EMAIL_ADDRESS] safely"
    assert "owner@example.test" not in repr(outbound)
    assert image_part.data_url.startswith("data:image/png;base64,")
    assert image.uri not in repr(outbound)


def test_gateway_blocks_image_hash_mismatch_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    object_store = LocalKnowledgeObjectStore(tmp_path / "objects")
    image = object_store.write_binary_artifact(_PNG_BYTES, namespace="images")
    transport = CapturingTransport(payloads=[])
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "external_models_enabled": True,
                "knowledge_object_store_path": str(object_store.root),
            }
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="hash mismatch"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="safe prompt",
                parts=(
                    ImagePart(
                        artifact_uri=image.uri,
                        sha256="0" * 64,
                        media_type="image/png",
                    ),
                ),
            )
        )

    assert transport.payloads == []


def test_gateway_fails_closed_for_unsupported_image_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    transport = CapturingTransport(payloads=[])
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "external_models_enabled": True,
                "knowledge_object_store_path": str(tmp_path / "objects"),
            }
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="unsupported image artifact source"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="safe prompt",
                parts=(
                    ImagePart(
                        artifact_uri="artifact://images/one",
                        sha256="a" * 64,
                        media_type="image/png",
                    ),
                ),
            )
        )

    assert transport.payloads == []


def test_openai_compatible_transport_never_serializes_raw_image_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> Any:
        del args
        requests.append(kwargs["json"])

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"choices": [{"message": {"content": "ok"}}]}

        return FakeResponse()

    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    monkeypatch.setattr("zhiheng.models._transports.httpx.post", fake_post)
    raw_uri = "file:///private/object-store/images/raw.png"
    data_url = "data:image/png;base64,ZmFrZQ=="

    OpenAICompatibleChatTransport(secret_store=EnvironmentSecretStore()).complete(
        route=TransportRoute(
            provider_id="provider-openai",
            provider_kind="openai-compatible",
            model_id="gpt-test",
            endpoint_url="https://models.example.test/v1",
            endpoint_origin="https://models.example.test",
            policy_revision="policy-rev-1",
            secret_ref="env:ZHIHENG_PRIVATE_TEST_SECRET",
        ),
        payload=_ApprovedOutboundPayload(
            text="safe prompt",
            payload_hash=sha256_text("safe prompt"),
            approval_id="approval-1",
            audit_id="audit-1",
            parts=(
                _ApprovedImagePart(
                    data_url=data_url,
                    media_type="image/png",
                    sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
                    detail="auto",
                ),
            ),
        ),
    )

    request_body = repr(requests[0])
    assert data_url in request_body
    assert raw_uri not in request_body
    assert "file://" not in request_body
