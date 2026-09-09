from __future__ import annotations

from pathlib import Path

from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts


def stored_text_artifacts(tmp_path: Path, text_value: str) -> StoredTextArtifacts:
    store = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store")
    artifacts = store.write_text_artifacts(text_value)
    store.verify_text_artifacts(artifacts)
    return artifacts
