"""Formal retrieval qualification shared by read APIs and projections."""

from __future__ import annotations


def formal_searchable_sql(knowledge_alias: str = "ko") -> str:
    """Return a SQL predicate for a formally searchable knowledge object.

    Qualification is deliberately stricter than merely having parsed chunks: the
    current formal version and active evidence must be present, a completed index
    job must exist, and every serving chunk must have a row in the active
    retrieval generation whose physical index is available.
    """
    if not knowledge_alias.replace("_", "").isalnum():
        raise ValueError("invalid SQL alias")
    object_id = f"{knowledge_alias}.id"
    current_version = f"{knowledge_alias}.current_version_id"
    return f"""
    EXISTS (
      SELECT 1
      FROM jobs completed_index
      WHERE completed_index.job_type = 'knowledge.index'
        AND completed_index.status = 'completed'
        AND (
          json_extract(completed_index.payload_json, '$.knowledge_object_id') = {object_id}
          OR json_extract(completed_index.payload_json, '$.aggregate_id') = {object_id}
        )
    )
    AND EXISTS (
      SELECT 1
      FROM knowledge_versions current_kv
      JOIN content_versions current_cv
        ON current_cv.id = current_kv.content_version_id
       AND current_cv.status = 'active'
      JOIN evidence_objects current_eo
        ON current_eo.id = current_cv.evidence_object_id
       AND current_eo.status = 'active'
      WHERE current_kv.knowledge_object_id = {object_id}
        AND current_kv.id = {current_version}
    )
    AND EXISTS (
      SELECT 1
      FROM serving_chunks qualified_chunk
      WHERE qualified_chunk.source_id = {object_id}
    )
    AND EXISTS (
      SELECT 1
      FROM embedding_generations active_generation
      WHERE active_generation.index_status = 'active'
        AND active_generation.purpose = 'retrieval'
        AND active_generation.model_id <> ''
        AND active_generation.model_revision <> ''
        AND active_generation.dimension > 0
        AND active_generation.physical_index_ref IS NOT NULL
        AND active_generation.physical_index_ref <> ''
        AND NOT EXISTS (
          SELECT 1
          FROM serving_chunks missing_chunk
          WHERE missing_chunk.source_id = {object_id}
            AND NOT EXISTS (
              SELECT 1
              FROM chunk_embeddings matching_embedding
              WHERE matching_embedding.chunk_id = missing_chunk.id
                AND matching_embedding.generation_id = active_generation.id
                AND matching_embedding.source_version_id = missing_chunk.source_version_id
                AND matching_embedding.visibility_scope = missing_chunk.visibility_scope
                AND matching_embedding.confirmation_generation = (
                  missing_chunk.confirmation_generation
                )
            )
        )
    )
    """
