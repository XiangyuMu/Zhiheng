from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol, cast

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id, sha256_json
from zhiheng.retrieval.contracts import RetrievalCandidate, RetrievalSource


@dataclass(frozen=True)
class EmbeddingGeneration:
    id: str
    model_id: str
    model_revision: str
    dimension: int
    normalize: bool
    index_status: str
    purpose: str
    physical_index_ref: str | None


class QueryEmbeddingPort(Protocol):
    def embed_query(
        self,
        query: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]: ...


class SqliteVecUnavailableError(RuntimeError):
    pass


def pack_embedding(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


class SqliteVecAdapter:
    _index_ref_prefix = "sqlite_vec:"

    def load(self, session: Session) -> None:
        sqlite_vec = self._sqlite_vec_module()

        dbapi_connection = self._dbapi_connection(session)
        enable_load_extension = getattr(dbapi_connection, "enable_load_extension", None)
        if enable_load_extension is not None:
            enable_load_extension(True)
        try:
            sqlite_vec.load(dbapi_connection)
        finally:
            if enable_load_extension is not None:
                enable_load_extension(False)

    def create_empty_index(self, session: Session, *, generation_id: str, dimension: int) -> str:
        self.load(session)
        table_name = self._table_name(generation_id)
        session.connection().exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")
        session.connection().exec_driver_sql(
            f"CREATE VIRTUAL TABLE {table_name} USING vec0(embedding float[{dimension}])"
        )
        return f"{self._index_ref_prefix}{table_name}"

    def insert_embeddings(
        self,
        session: Session,
        *,
        physical_index_ref: str,
        rows: Sequence[tuple[int, Sequence[float]]],
    ) -> None:
        if not rows:
            return
        self.load(session)
        table_name = self._table_name_from_ref(physical_index_ref)
        sqlite_vec = self._sqlite_vec_module()
        session.connection().exec_driver_sql(
            f"INSERT INTO {table_name}(rowid, embedding) VALUES (?, ?)",
            [
                (rowid, sqlite_vec.serialize_float32([float(value) for value in values]))
                for rowid, values in rows
            ],
        )

    def search(
        self,
        session: Session,
        *,
        physical_index_ref: str,
        query_embedding: Sequence[float],
        limit: int,
    ) -> list[tuple[int, float]]:
        if limit <= 0:
            return []
        self.load(session)
        table_name = self._table_name_from_ref(physical_index_ref)
        sqlite_vec = self._sqlite_vec_module()
        rows = session.connection().exec_driver_sql(
            f"""
            SELECT rowid, distance
            FROM {table_name}
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (sqlite_vec.serialize_float32([float(value) for value in query_embedding]), limit),
        )
        return [(int(row[0]), float(row[1])) for row in rows]

    @staticmethod
    def _sqlite_vec_module() -> Any:
        try:
            return import_module("sqlite_vec")
        except ImportError as exc:
            raise SqliteVecUnavailableError(
                "sqlite-vec is required for vector indexing; install sqlite-vec==0.1.9"
            ) from exc

    @classmethod
    def _table_name(cls, generation_id: str) -> str:
        suffix = "".join(character if character.isalnum() else "_" for character in generation_id)
        return f"vec_chunks_{suffix}"

    @classmethod
    def _table_name_from_ref(cls, physical_index_ref: str) -> str:
        if not physical_index_ref.startswith(cls._index_ref_prefix):
            raise ValueError("unsupported vector physical index ref")
        table_name = physical_index_ref.removeprefix(cls._index_ref_prefix)
        if not table_name.startswith("vec_chunks_") or not all(
            character.isalnum() or character == "_" for character in table_name
        ):
            raise ValueError("invalid vector physical index ref")
        return table_name

    @staticmethod
    def _dbapi_connection(session: Session) -> Any:
        proxied = session.connection().connection
        driver_connection = getattr(proxied, "driver_connection", None)
        if driver_connection is not None:
            return driver_connection
        return cast(Any, proxied).connection


class VectorIndexRepository:
    def __init__(self, adapter: SqliteVecAdapter | None = None) -> None:
        self._adapter = adapter or SqliteVecAdapter()

    def create_generation(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
        normalize: bool = True,
    ) -> str:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        if not purpose:
            raise ValueError("embedding purpose cannot be empty")

        generation_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO embedding_generations (
                  id, model_id, model_revision, dimension, normalize, index_status, purpose
                )
                VALUES (
                  :id, :model_id, :model_revision, :dimension, :normalize, 'building', :purpose
                )
                """
            ),
            {
                "id": generation_id,
                "model_id": model_id,
                "model_revision": model_revision,
                "dimension": dimension,
                "normalize": normalize,
                "purpose": purpose,
            },
        )
        return generation_id

    def rebuild_generation(
        self,
        session: Session,
        generation_id: str,
        embeddings_by_chunk_id: Mapping[str, Sequence[float]],
    ) -> int:
        generation = self._generation(session, generation_id)
        if generation.index_status == "active":
            raise ValueError("active generation cannot be rebuilt")
        physical_index_ref = self._adapter.create_empty_index(
            session,
            generation_id=generation_id,
            dimension=generation.dimension,
        )
        serving_rows = session.execute(
            text(
                """
                SELECT
                  c.rowid,
                  s.id,
                  s.source_version_id,
                  s.visibility_scope,
                  s.confirmation_generation
                FROM serving_chunks s
                JOIN chunks c ON c.id = s.id
                ORDER BY s.id
                """
            )
        ).mappings()

        session.execute(
            text("DELETE FROM chunk_embeddings WHERE generation_id = :generation_id"),
            {"generation_id": generation_id},
        )
        inserted = 0
        physical_rows: list[tuple[int, Sequence[float]]] = []
        source_manifest_rows: list[dict[str, object]] = []
        for row in serving_rows:
            chunk_id = str(row["id"])
            values = embeddings_by_chunk_id.get(chunk_id)
            if values is None:
                continue
            if len(values) != generation.dimension:
                raise ValueError("embedding dimension does not match generation")
            session.execute(
                text(
                    """
                    INSERT INTO chunk_embeddings (
                      chunk_id, generation_id, embedding, source_version_id,
                      visibility_scope, confirmation_generation
                    )
                    VALUES (
                      :chunk_id, :generation_id, :embedding, :source_version_id,
                      :visibility_scope, :confirmation_generation
                    )
                    """
                ),
                {
                    "chunk_id": chunk_id,
                    "generation_id": generation_id,
                    "embedding": pack_embedding(values),
                    "source_version_id": row["source_version_id"],
                    "visibility_scope": row["visibility_scope"],
                    "confirmation_generation": row["confirmation_generation"],
                },
            )
            inserted += 1
            physical_rows.append((int(row["rowid"]), values))
            source_manifest_rows.append(
                {
                    "chunk_id": chunk_id,
                    "source_version_id": str(row["source_version_id"]),
                    "confirmation_generation": int(row["confirmation_generation"]),
                }
            )

        self._adapter.insert_embeddings(
            session,
            physical_index_ref=physical_index_ref,
            rows=physical_rows,
        )

        session.execute(
            text(
                """
                UPDATE embedding_generations
                SET index_status = 'built',
                    physical_index_ref = :physical_index_ref,
                    built_count = :built_count,
                    source_manifest_hash = :source_manifest_hash
                WHERE id = :generation_id
                """
            ),
            {
                "generation_id": generation_id,
                "physical_index_ref": physical_index_ref,
                "built_count": inserted,
                "source_manifest_hash": sha256_json({"rows": source_manifest_rows}),
            },
        )
        return inserted

    def activate_generation(self, session: Session, generation_id: str) -> None:
        generation = self._generation(session, generation_id)
        if generation.index_status != "built":
            raise ValueError("only built generations can be activated")
        if generation.physical_index_ref is None:
            raise ValueError("generation has no physical vector index")
        session.execute(
            text(
                """
                UPDATE embedding_generations
                SET index_status = 'archived'
                WHERE model_id = :model_id
                  AND purpose = :purpose
                  AND index_status = 'active'
                  AND id <> :generation_id
                """
            ),
            {
                "model_id": generation.model_id,
                "purpose": generation.purpose,
                "generation_id": generation_id,
            },
        )
        session.execute(
            text(
                """
                UPDATE embedding_generations
                SET index_status = 'active', activated_at = CURRENT_TIMESTAMP
                WHERE id = :generation_id
                """
            ),
            {"generation_id": generation_id},
        )

    def active_generation_id(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
    ) -> str | None:
        row = session.execute(
            text(
                """
                SELECT id
                FROM embedding_generations
                WHERE model_id = :model_id
                  AND model_revision = :model_revision
                  AND dimension = :dimension
                  AND purpose = :purpose
                  AND index_status = 'active'
                """
            ),
            {
                "model_id": model_id,
                "model_revision": model_revision,
                "dimension": dimension,
                "purpose": purpose,
            },
        ).first()
        return str(row[0]) if row is not None else None

    def active_generation(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
    ) -> EmbeddingGeneration | None:
        return self._active_generation(
            session,
            model_id=model_id,
            model_revision=model_revision,
            dimension=dimension,
            purpose=purpose,
        )

    def search_active(
        self,
        session: Session,
        query_embedding: Sequence[float],
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
        limit: int = 10,
    ) -> list[RetrievalCandidate]:
        if not query_embedding:
            raise ValueError("query_embedding cannot be empty")
        if len(query_embedding) != dimension:
            raise ValueError("query embedding dimension does not match requested dimension")
        generation = self._active_generation(
            session,
            model_id=model_id,
            model_revision=model_revision,
            dimension=dimension,
            purpose=purpose,
        )
        if generation is None:
            return []
        if generation.physical_index_ref is None:
            raise ValueError("active generation has no physical vector index")

        vector_rows = self._adapter.search(
            session,
            physical_index_ref=generation.physical_index_ref,
            query_embedding=query_embedding,
            limit=limit,
        )
        if not vector_rows:
            return []

        rowids = [rowid for rowid, _ in vector_rows]
        scores_by_rowid = {rowid: 1.0 / (1.0 + distance) for rowid, distance in vector_rows}
        metadata_rows = session.execute(
            text(
                """
                SELECT
                  c.rowid,
                  c.source_type,
                  c.source_id,
                  c.source_version_id,
                  c.id AS chunk_id,
                  c.confirmation_generation
                FROM chunks c
                JOIN serving_chunks s ON s.id = c.id
                JOIN chunk_embeddings ce
                  ON ce.chunk_id = c.id
                 AND ce.generation_id = :generation_id
                 AND ce.source_version_id = c.source_version_id
                 AND ce.confirmation_generation = c.confirmation_generation
                WHERE c.rowid IN :rowids
                  AND (
                    c.source_type <> 'knowledge_object'
                    OR EXISTS (
                    SELECT 1
                    FROM jobs completed_index
                    WHERE completed_index.job_type = 'knowledge.index'
                      AND completed_index.status = 'completed'
                      AND (
                        json_extract(completed_index.payload_json, '$.knowledge_object_id')
                          = c.source_id
                        OR json_extract(completed_index.payload_json, '$.aggregate_id')
                          = c.source_id
                      )
                    )
                  )
                  AND (
                    c.source_type <> 'knowledge_object'
                    OR EXISTS (
                      SELECT 1
                      FROM knowledge_objects ko
                      JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                      JOIN content_versions cv ON cv.id = kv.content_version_id
                      JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                      WHERE ko.id = c.source_id
                        AND ko.current_version_id = c.source_version_id
                        AND cv.status = 'active'
                        AND eo.status = 'active'
                    )
                  )
                """
            ).bindparams(bindparam("rowids", expanding=True)),
            {"generation_id": generation.id, "rowids": rowids},
        ).mappings()
        metadata_by_rowid = {int(row["rowid"]): row for row in metadata_rows}

        candidates: list[RetrievalCandidate] = []
        for rowid, _distance in vector_rows:
            row = metadata_by_rowid.get(rowid)
            if row is None:
                continue
            candidates.append(
                RetrievalCandidate(
                    source_type=str(row["source_type"]),
                    source_id=str(row["source_id"]),
                    source_version_id=str(row["source_version_id"]),
                    chunk_id=str(row["chunk_id"]),
                    confirmation_generation=int(row["confirmation_generation"]),
                    generation=generation.id,
                    rank=len(candidates) + 1,
                    score=scores_by_rowid[rowid],
                    retriever=RetrievalSource.VECTOR,
                )
            )
        return candidates

    def search_query(
        self,
        session: Session,
        query: str,
        *,
        embedder: QueryEmbeddingPort,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str = "retrieval",
        normalize: bool = True,
        limit: int = 10,
    ) -> list[RetrievalCandidate]:
        query_embedding = embedder.embed_query(
            query,
            model_id=model_id,
            model_revision=model_revision,
            dimension=dimension,
            normalize=normalize,
        )
        return self.search_active(
            session,
            query_embedding,
            model_id=model_id,
            model_revision=model_revision,
            dimension=dimension,
            purpose=purpose,
            limit=limit,
        )

    def _generation(self, session: Session, generation_id: str) -> EmbeddingGeneration:
        row = (
            session.execute(
                text(
                    """
                SELECT
                  id, model_id, model_revision, dimension, normalize, index_status, purpose,
                  physical_index_ref
                FROM embedding_generations
                WHERE id = :generation_id
                """
                ),
                {"generation_id": generation_id},
            )
            .mappings()
            .one()
        )
        return self._to_generation(row)

    def _active_generation(
        self,
        session: Session,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        purpose: str,
    ) -> EmbeddingGeneration | None:
        row = (
            session.execute(
                text(
                    """
                SELECT
                  id, model_id, model_revision, dimension, normalize, index_status, purpose,
                  physical_index_ref
                FROM embedding_generations
                WHERE model_id = :model_id
                  AND model_revision = :model_revision
                  AND dimension = :dimension
                  AND purpose = :purpose
                  AND index_status = 'active'
                """
                ),
                {
                    "model_id": model_id,
                    "model_revision": model_revision,
                    "dimension": dimension,
                    "purpose": purpose,
                },
            )
            .mappings()
            .first()
        )
        return None if row is None else self._to_generation(row)

    @staticmethod
    def _to_generation(row: RowMapping) -> EmbeddingGeneration:
        return EmbeddingGeneration(
            id=str(row["id"]),
            model_id=str(row["model_id"]),
            model_revision=str(row["model_revision"]),
            dimension=int(row["dimension"]),
            normalize=bool(row["normalize"]),
            index_status=str(row["index_status"]),
            purpose=str(row["purpose"]),
            physical_index_ref=(
                None if row["physical_index_ref"] is None else str(row["physical_index_ref"])
            ),
        )
