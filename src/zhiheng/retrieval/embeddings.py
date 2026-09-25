from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

BGE_M3_MODEL_ID = "BAAI/bge-m3"
BGE_M3_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_M3_DIMENSION = 1024
BGE_M3_PURPOSE = "retrieval"
BGE_M3_NORMALIZE = True


class QueryEmbeddingUnavailableError(RuntimeError):
    pass


class BgeM3QueryEmbedder:
    def __init__(
        self,
        *,
        model_id: str = BGE_M3_MODEL_ID,
        model_revision: str = BGE_M3_MODEL_REVISION,
        dimension: int = BGE_M3_DIMENSION,
        normalize: bool = BGE_M3_NORMALIZE,
        allow_model_download: bool = False,
        device: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.model_revision = model_revision
        self.dimension = dimension
        self.normalize = normalize
        self.allow_model_download = allow_model_download
        self.device = device
        self._model: Any | None = None

    def embed_query(
        self,
        query: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        if model_id != self.model_id or model_revision != self.model_revision:
            raise ValueError("query embedder does not match active embedding generation")
        if dimension != self.dimension:
            raise ValueError("query embedder dimension does not match active embedding generation")
        if normalize != self.normalize:
            raise ValueError(
                "query embedder normalization does not match active embedding generation"
            )
        if not query.strip():
            raise ValueError("query cannot be empty")

        encoded = self._load_model().encode(
            [query],
            normalize_embeddings=normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vector = [float(value) for value in encoded[0].tolist()]
        if len(vector) != dimension:
            raise ValueError("model returned an unexpected embedding dimension")
        return _l2_normalize(vector) if normalize else vector

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from importlib import import_module

            SentenceTransformer = import_module("sentence_transformers").SentenceTransformer
        except ImportError as exc:
            raise QueryEmbeddingUnavailableError(
                "local query embeddings require the 'local-embeddings' extra "
                "(sentence-transformers with BAAI/bge-m3)"
            ) from exc

        kwargs: dict[str, Any] = {
            "revision": self.model_revision,
            "local_files_only": not self.allow_model_download,
        }
        if self.device is not None:
            kwargs["device"] = self.device
        try:
            self._model = SentenceTransformer(self.model_id, **kwargs)
        except (OSError, ValueError, RuntimeError) as exc:
            raise QueryEmbeddingUnavailableError(
                "local query embeddings are unavailable because the model snapshot is missing "
                "and downloads are disabled"
            ) from exc
        return self._model


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]
