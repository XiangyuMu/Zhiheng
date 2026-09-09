from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.knowledge.object_store import (
    LocalKnowledgeObjectStore,
    StoredTextArtifacts,
    knowledge_object_store_for_settings,
)
from zhiheng.knowledge.repository import (
    ExternalKnowledgeCandidateInput,
    IngestedKnowledge,
    KnowledgeRepository,
    KnowledgeUserAuthority,
    TextEvidenceInput,
)

__all__ = ["KnowledgeIngestionService"]


@dataclass(frozen=True)
class KnowledgeIngestionService:
    settings: Settings
    repository: KnowledgeRepository = KnowledgeRepository()
    object_store: LocalKnowledgeObjectStore | None = None

    def ingest_user_text(
        self,
        session_factory: sessionmaker[Session],
        item: TextEvidenceInput,
        *,
        user_authority: KnowledgeUserAuthority,
    ) -> IngestedKnowledge:
        stored_artifacts = self.prepare_text_artifacts(item.text)
        with session_factory.begin() as session:
            return self.ingest_prepared_user_text(
                session,
                item,
                user_authority=user_authority,
                stored_artifacts=stored_artifacts,
            )

    def create_external_candidate(
        self,
        session_factory: sessionmaker[Session],
        item: ExternalKnowledgeCandidateInput,
    ) -> IngestedKnowledge:
        stored_artifacts = self.prepare_text_artifacts(item.text)
        with session_factory.begin() as session:
            return self.create_prepared_external_candidate(
                session,
                item,
                stored_artifacts=stored_artifacts,
            )

    def prepare_text_artifacts(self, text_value: str) -> StoredTextArtifacts:
        return self._write_and_verify(text_value)

    def verify_prepared_text_artifacts(self, stored_artifacts: StoredTextArtifacts) -> None:
        store = self.object_store or knowledge_object_store_for_settings(self.settings)
        store.verify_text_artifacts(stored_artifacts)

    def ingest_prepared_user_text(
        self,
        session: Session,
        item: TextEvidenceInput,
        *,
        user_authority: KnowledgeUserAuthority,
        stored_artifacts: StoredTextArtifacts,
    ) -> IngestedKnowledge:
        return self.repository.ingest_text(
            session,
            item,
            user_authority=user_authority,
            stored_artifacts=stored_artifacts,
        )

    def create_prepared_external_candidate(
        self,
        session: Session,
        item: ExternalKnowledgeCandidateInput,
        *,
        stored_artifacts: StoredTextArtifacts,
    ) -> IngestedKnowledge:
        return self.repository.create_external_candidate(
            session,
            item,
            stored_artifacts=stored_artifacts,
        )

    def _write_and_verify(self, text_value: str) -> StoredTextArtifacts:
        store = self.object_store or knowledge_object_store_for_settings(self.settings)
        stored_artifacts = store.write_text_artifacts(text_value)
        store.verify_text_artifacts(stored_artifacts)
        return stored_artifacts
