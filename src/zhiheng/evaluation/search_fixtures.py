"""Helpers for building formally searchable, deterministic evaluation fixtures."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id
from zhiheng.knowledge import KnowledgeRepository


def mark_formal_knowledge_indexed(
    session: Session, knowledge_object_id: str, *, with_vectors: bool = True
) -> None:
    """Publish the durable indexing completion required by formal retrieval gates.

    Evaluation data is inserted directly to keep setup deterministic. Recording the
    same completed job proof that production indexing produces ensures lexical
    retrieval still exercises the real serving and authorization predicates.
    """
    from zhiheng.jobs.knowledge_indexing import KnowledgeJobRepository
    from zhiheng.retrieval.vector_index import VectorIndexRepository

    session.execute(
        text(
            """
            INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status)
            VALUES (:id, 'knowledge.index', :idempotency_key, :payload, 'pending')
            """
        ),
        {
            "id": new_id(),
            "idempotency_key": f"evaluation:knowledge.index:{knowledge_object_id}",
            "payload": json_text(
                {
                    "knowledge_object_id": knowledge_object_id,
                    "source": "deterministic-evaluation-fixture",
                }
            ),
        },
    )

    repository = KnowledgeJobRepository()
    jobs = repository.claim_available(session, worker_id="evaluation-fixture", limit=1)
    if len(jobs) != 1 or jobs[0].payload.get("knowledge_object_id") != knowledge_object_id:
        raise ValueError("evaluation fixture must not contain unrelated pending jobs")
    indexed = KnowledgeRepository().rebuild_fts_index(session)
    if not repository.complete(session, jobs[0], result={"fts_indexed": indexed}):
        raise ValueError("evaluation index completion lost its claim")
    if not with_vectors:
        return
    vector = VectorIndexRepository()
    generation_id = vector.create_generation(
        session,
        model_id="evaluation-embedding",
        model_revision="deterministic-v1",
        dimension=2,
    )
    chunk_ids = (
        session.execute(
            text("SELECT id FROM serving_chunks"),
        )
        .scalars()
        .all()
    )
    if not chunk_ids:
        raise ValueError("evaluation fixture must contain serving chunks")
    vector.rebuild_generation(
        session,
        generation_id,
        {str(chunk_id): [1.0, 0.0] for chunk_id in chunk_ids},
    )
    vector.activate_generation(session, generation_id)
