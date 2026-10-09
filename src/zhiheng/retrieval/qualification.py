"""Formal retrieval qualification shared by read APIs and projections."""

from __future__ import annotations


def active_retrieval_generation_sql(
    source_id_sql: str,
    generation_alias: str = "active_generation",
) -> str:
    """Return SQL predicate for an active retrieval generation serving a source."""
    if not generation_alias.replace("_", "").isalnum():
        raise ValueError("invalid SQL alias")
    return f"""
        {generation_alias}.index_status = 'active'
        AND {generation_alias}.purpose = 'retrieval'
        AND {generation_alias}.model_id <> ''
        AND {generation_alias}.model_revision <> ''
        AND {generation_alias}.dimension > 0
        AND {generation_alias}.physical_index_ref IS NOT NULL
        AND {generation_alias}.physical_index_ref <> ''
        AND NOT EXISTS (
          SELECT 1
          FROM serving_chunks missing_chunk
          WHERE missing_chunk.source_id = {source_id_sql}
            AND NOT EXISTS (
              SELECT 1
              FROM chunk_embeddings matching_embedding
              WHERE matching_embedding.chunk_id = missing_chunk.id
                AND matching_embedding.generation_id = {generation_alias}.id
                AND matching_embedding.source_version_id = missing_chunk.source_version_id
                AND matching_embedding.visibility_scope = missing_chunk.visibility_scope
                AND matching_embedding.confirmation_generation = (
                  missing_chunk.confirmation_generation
                )
            )
        )
    """


def pdf_source_qualified_sql(
    content_alias: str = "current_cv",
    evidence_alias: str = "current_eo",
) -> str:
    """Return SQL predicate for parser-backed PDF evidence readiness."""
    for alias in (content_alias, evidence_alias):
        if not alias.replace("_", "").isalnum():
            raise ValueError("invalid SQL alias")
    return f"""
    (
      {evidence_alias}.media_type <> 'application/pdf'
      OR EXISTS (
        SELECT 1
        FROM pdf_tasks parsed_pdf_task
        JOIN pdf_parse_attempts succeeded_pdf_attempt
          ON succeeded_pdf_attempt.task_id = parsed_pdf_task.id
         AND succeeded_pdf_attempt.evidence_object_id = {evidence_alias}.id
         AND succeeded_pdf_attempt.status = 'succeeded'
        WHERE parsed_pdf_task.evidence_object_id = {evidence_alias}.id
          AND parsed_pdf_task.state = 'parsed'
          AND EXISTS (
            SELECT 1
            FROM evidence_blocks pdf_block
            WHERE pdf_block.attempt_id = succeeded_pdf_attempt.id
              AND pdf_block.status = 'formal'
              AND pdf_block.content_version_id = {content_alias}.id
          )
      )
    )
    """


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
        AND {pdf_source_qualified_sql("current_cv", "current_eo")}
    )
    AND EXISTS (
      SELECT 1
      FROM serving_chunks qualified_chunk
      WHERE qualified_chunk.source_id = {object_id}
        AND qualified_chunk.source_version_id = {current_version}
    )
    AND NOT EXISTS (
      SELECT 1
      FROM serving_chunks stale_chunk
      WHERE stale_chunk.source_id = {object_id}
        AND stale_chunk.source_version_id <> {current_version}
    )
    AND EXISTS (
      SELECT 1
      FROM embedding_generations active_generation
      WHERE {active_retrieval_generation_sql(object_id)}
    )
    """
