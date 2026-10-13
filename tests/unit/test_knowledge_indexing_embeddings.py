from __future__ import annotations

from typing import Any

import pytest

from zhiheng.jobs import knowledge_indexing
from zhiheng.models.errors import EmbeddingTransportError
from zhiheng.secrets import InMemoryMasterKeyBackend, ProviderSecretStore


class _FakeTransport:
    instances: list[_FakeTransport] = []

    def __init__(self, _secret_store: object) -> None:
        self.calls: list[list[str]] = []
        self.response_mode = "valid"
        self.fail_on_call: int | None = None
        self.__class__.instances.append(self)

    def embed(self, *, route: object, texts: list[str]) -> list[list[float]]:
        del route
        self.calls.append(texts)
        if self.fail_on_call == len(self.calls):
            raise EmbeddingTransportError("provider unavailable")
        if self.response_mode == "transport-error":
            raise EmbeddingTransportError("provider unavailable")
        if self.response_mode == "wrong-count":
            return [[1.0, 2.0]]
        if self.response_mode == "wrong-dimension":
            return [[1.0] for _ in texts]
        return [[float(len(value)), 1.0] for value in texts]


def _embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[knowledge_indexing.ProviderTextEmbedder, _FakeTransport]:
    _FakeTransport.instances.clear()
    monkeypatch.setattr(knowledge_indexing, "OpenAIEmbeddingsTransport", _FakeTransport)
    embedder = knowledge_indexing.ProviderTextEmbedder(
        {
            "provider_id": "provider-test",
            "provider_kind": "openai",
            "endpoint_url": "https://example.test/v1",
            "endpoint_origin": "manual",
            "secret_ref": "env:TEST_KEY",
        },
        ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
    )
    return embedder, _FakeTransport.instances[-1]


def _embed(
    embedder: knowledge_indexing.ProviderTextEmbedder,
    texts: list[str],
) -> list[Any]:
    return embedder.embed_texts(
        texts,
        model_id="embedding-test",
        model_revision="test-revision",
        dimension=2,
        normalize=False,
    )


def test_provider_embedding_batches_and_maps_duplicate_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, transport = _embedder(monkeypatch)
    texts = [f"text-{index}" for index in range(65)]
    texts[40] = texts[2]

    vectors = _embed(embedder, texts)

    assert [len(batch) for batch in transport.calls] == [32, 32]
    assert sum(len(batch) for batch in transport.calls) == 64
    assert vectors[2] == vectors[40]
    assert len(vectors) == len(texts)


def test_provider_embedding_empty_input_does_not_call_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, transport = _embedder(monkeypatch)

    assert _embed(embedder, []) == []
    assert transport.calls == []


@pytest.mark.parametrize("response_mode", ["wrong-count", "wrong-dimension"])
def test_provider_embedding_rejects_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    response_mode: str,
) -> None:
    embedder, transport = _embedder(monkeypatch)
    transport.response_mode = response_mode

    with pytest.raises(ValueError, match="dimension"):
        _embed(embedder, ["one", "two"])

    assert len(transport.calls) == 1


def test_provider_embedding_propagates_transport_error_without_per_text_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, transport = _embedder(monkeypatch)
    transport.response_mode = "transport-error"

    with pytest.raises(EmbeddingTransportError):
        _embed(embedder, ["one", "two"])

    assert len(transport.calls) == 1


def test_provider_embedding_does_not_retry_a_failed_later_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, transport = _embedder(monkeypatch)
    transport.fail_on_call = 2
    texts = [f"text-{index}" for index in range(33)]

    with pytest.raises(EmbeddingTransportError):
        _embed(embedder, texts)

    assert [len(batch) for batch in transport.calls] == [32, 1]
