from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from zhiheng.db.session import session_scope
from zhiheng.jobs.worker_heartbeats import record_worker_heartbeat
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts


def stored_text_artifacts(tmp_path: Path, text_value: str) -> StoredTextArtifacts:
    store = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store")
    artifacts = store.write_text_artifacts(text_value)
    store.verify_text_artifacts(artifacts)
    return artifacts


def announce_pdf_parser_worker(
    session_factory: sessionmaker[Session], backend: str = "deepdoc"
) -> None:
    """Record a live worker heartbeat declaring a PDF parser, as a serving worker would."""

    with session_scope(session_factory) as session:
        record_worker_heartbeat(
            session,
            worker_id="test-pdf-parser-worker",
            role="worker",
            capabilities={
                "pdf_parsing": {
                    "declared": backend,
                    "configured": True,
                    "detail": f"PDF parsing uses {backend}",
                }
            },
        )
