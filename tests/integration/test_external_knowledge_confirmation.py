from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import (
    ExternalKnowledgeCandidateInput,
    KnowledgeRepository,
    KnowledgeUserAuthority,
    TextEvidenceInput,
)
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts


def _migrated_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _client(tmp_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng-api.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    app = create_app(settings)
    return TestClient(app), session_factory


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo-user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _engine_for_session_factory(session_factory: sessionmaker[Session]) -> Engine:
    session = session_factory()
    try:
        bind = session.get_bind()
    finally:
        session.close()
    if not isinstance(bind, Engine):
        raise TypeError("expected SQLAlchemy Engine bind")
    return bind


class TransactionTrackingObjectStore(LocalKnowledgeObjectStore):
    def __init__(self, root: Path, active_transaction_count: list[int]) -> None:
        super().__init__(root)
        self._active_transaction_count = active_transaction_count
        self.write_transaction_depths: list[int] = []
        self.verify_transaction_depths: list[int] = []

    def write_text_artifacts(self, text_value: str) -> StoredTextArtifacts:
        self.write_transaction_depths.append(self._active_transaction_count[0])
        return super().write_text_artifacts(text_value)

    def verify_text_artifacts(self, artifacts: StoredTextArtifacts) -> None:
        self.verify_transaction_depths.append(self._active_transaction_count[0])
        super().verify_text_artifacts(artifacts)


def _track_active_transactions(session_factory: sessionmaker[Session]) -> list[int]:
    active_transaction_count = [0]
    engine = _engine_for_session_factory(session_factory)

    @event.listens_for(engine, "begin")
    def _increment_transaction_depth(_connection: object) -> None:
        active_transaction_count[0] += 1

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def _decrement_transaction_depth(_connection: object) -> None:
        active_transaction_count[0] -= 1

    return active_transaction_count


def _external_candidate(
    text_value: str = "外部网页知识必须先经过用户确认。",
) -> ExternalKnowledgeCandidateInput:
    return ExternalKnowledgeCandidateInput(
        title="外部网页候选",
        primary_domain_id="technology.ai",
        text=text_value,
        source_url="https://example.invalid/article",
        discovered_by="knowledge-gap-recommender",
        source_metadata={"discovery_query": "synthetic external source"},
        summary="external candidate fixture",
    )


def test_external_discovery_is_candidate_only_until_user_confirmation(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    item = _external_candidate()
    artifacts = stored_text_artifacts(tmp_path, item.text)

    with session_scope(session_factory) as session:
        candidate = repository.create_external_candidate(
            session,
            item,
            stored_artifacts=artifacts,
        )
        request_payload = session.execute(
            text(
                """
                SELECT proposed_value_json
                FROM knowledge_confirmation_requests
                WHERE id = :request_id
                """
            ),
            {"request_id": candidate.confirmation_request_id},
        ).scalar_one()

        assert repository.search_formal_fts(session, "外部 网页") == []
        formal_count = session.execute(
            text("SELECT count(*) FROM current_formal_knowledge")
        ).scalar_one()
        assert session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one() == 0
        row = session.execute(
            text(
                """
                SELECT ko.lifecycle_status, ko.visibility_scope, ko.confirmation_generation,
                       c.status, c.visibility_scope, c.confirmation_generation,
                       eo.source_kind
                FROM knowledge_objects ko
                JOIN chunks c ON c.source_id = ko.id
                JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE ko.id = :id
                """
            ),
            {"id": candidate.knowledge_object_id},
        ).one()

    assert formal_count == 0
    assert "外部网页知识必须先经过用户确认" not in str(request_payload)
    assert "外部网页候选" not in str(request_payload)
    assert "example.invalid" not in str(request_payload)
    assert row == (
        "awaiting_user_confirmation",
        "candidate",
        0,
        "blocked_by_confirmation",
        "candidate",
        0,
        "external_discovered",
    )


def test_external_confirmation_binds_expected_content_before_formal_serving(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    text_value = "用户确认之后，外部网页知识才能进入正式检索。"
    item = _external_candidate(text_value)
    artifacts = stored_text_artifacts(tmp_path, item.text)

    with session_scope(session_factory) as session:
        candidate = repository.create_external_candidate(
            session,
            item,
            stored_artifacts=artifacts,
        )
        with pytest.raises(ValueError, match="content hash mismatch"):
            repository.confirm_external_candidate(
                session,
                candidate.knowledge_object_id,
                confirmation_request_id=str(candidate.confirmation_request_id),
                expected_content_sha256=sha256_text("different content"),
                user_authority=KnowledgeUserAuthority("synthetic-user"),
            )

        result = repository.confirm_external_candidate(
            session,
            candidate.knowledge_object_id,
            confirmation_request_id=str(candidate.confirmation_request_id),
            expected_content_sha256=sha256_text(text_value),
            user_authority=KnowledgeUserAuthority("synthetic-user"),
        )
        hits = repository.search_formal_fts(session, "用户 确认")
        event_types = set(session.execute(text("SELECT event_type FROM outbox_events")).scalars())
        decision = session.execute(
            text(
                """
                SELECT decided_by_user_id, final_value_json
                FROM knowledge_confirmation_decisions
                WHERE request_id = :request_id
                """
            ),
            {"request_id": result.confirmation_request_id},
        ).mappings().one()

    assert result.confirmation_generation == 1
    assert hits[0].source_id == candidate.knowledge_object_id
    assert event_types == {"knowledge_candidate.created", "knowledge_candidate.confirmed"}
    assert decision["decided_by_user_id"] == "synthetic-user"
    assert sha256_text(text_value) in str(decision["final_value_json"])
    assert text_value not in str(decision["final_value_json"])


def test_external_candidate_cannot_be_confirmed_twice(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    text_value = "重复确认不应创建第二个正式 generation。"
    item = _external_candidate(text_value)
    artifacts = stored_text_artifacts(tmp_path, item.text)

    with session_scope(session_factory) as session:
        candidate = repository.create_external_candidate(
            session,
            item,
            stored_artifacts=artifacts,
        )
        repository.confirm_external_candidate(
            session,
            candidate.knowledge_object_id,
            confirmation_request_id=str(candidate.confirmation_request_id),
            expected_content_sha256=sha256_text(text_value),
            user_authority=KnowledgeUserAuthority("synthetic-user"),
        )

        with pytest.raises(ValueError, match="not pending confirmation"):
            repository.confirm_external_candidate(
                session,
                candidate.knowledge_object_id,
                confirmation_request_id=str(candidate.confirmation_request_id),
                expected_content_sha256=sha256_text(text_value),
                user_authority=KnowledgeUserAuthority("synthetic-user"),
            )

        generation = session.execute(
            text("SELECT confirmation_generation FROM knowledge_objects WHERE id = :id"),
            {"id": candidate.knowledge_object_id},
        ).scalar_one()

    assert generation == 1


def test_formal_ingest_requires_user_authority_not_source_kind_only(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()

    with session_scope(session_factory) as session, pytest.raises(TypeError):
        repository.ingest_text(  # type: ignore[call-arg]
            session,
            TextEvidenceInput(
                title="伪装手动资料",
                primary_domain_id="technology.ai",
                text="仅把 source_kind 设置成 manual 不应构成用户授权。",
                source_kind="manual",
            ),
        )


def test_confirmation_request_binding_blocks_wrong_request_id(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    text_value = "确认请求必须绑定候选对象和内容。"
    first_item = _external_candidate(text_value)
    second_item = _external_candidate("另一条候选")
    first_artifacts = stored_text_artifacts(tmp_path, first_item.text)
    second_artifacts = stored_text_artifacts(tmp_path, second_item.text)

    with session_scope(session_factory) as session:
        first = repository.create_external_candidate(
            session,
            first_item,
            stored_artifacts=first_artifacts,
        )
        second = repository.create_external_candidate(
            session,
            second_item,
            stored_artifacts=second_artifacts,
        )

        with pytest.raises(ValueError, match="not pending for candidate"):
            repository.confirm_external_candidate(
                session,
                first.knowledge_object_id,
                confirmation_request_id=str(second.confirmation_request_id),
                expected_content_sha256=sha256_text(text_value),
                user_authority=KnowledgeUserAuthority("synthetic-user"),
            )


def test_knowledge_api_user_import_is_authenticated_and_idempotent(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    payload = {
        "title": "用户主动导入",
        "primary_domain_id": "technology.ai",
        "text": "用户主动导入的资料可以直接进入正式知识库。",
        "source_metadata": {"fixture": "synthetic"},
    }

    assert client.post("/v1/knowledge/imports", json=payload).status_code == 401
    csrf = _login(client)
    assert client.post(
        "/v1/knowledge/imports",
        json={**payload, "source_kind": "manual"},
        headers=_headers(csrf, "import-extra-field"),
    ).status_code == 422

    first = client.post(
        "/v1/knowledge/imports",
        json=payload,
        headers=_headers(csrf, "import-1"),
    )
    replay = client.post(
        "/v1/knowledge/imports",
        json=payload,
        headers=_headers(csrf, "import-1"),
    )
    conflict = client.post(
        "/v1/knowledge/imports",
        json={**payload, "title": "不同 payload"},
        headers=_headers(csrf, "import-1"),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    with session_scope(session_factory) as session:
        current_count = session.execute(
            text("SELECT count(*) FROM current_formal_knowledge")
        ).scalar_one()
    assert current_count == 1


def test_knowledge_api_import_writes_and_verifies_objects_outside_db_transactions(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    active_transactions = _track_active_transactions(session_factory)
    object_store = TransactionTrackingObjectStore(tmp_path / "objects", active_transactions)
    cast(Any, client.app).state.knowledge_object_store = object_store
    csrf = _login(client)
    payload = {
        "title": "事务外对象写入",
        "primary_domain_id": "technology.ai",
        "text": "对象文件必须在数据库事务外写入并验字节。",
        "source_metadata": {"fixture": "synthetic"},
    }

    response = client.post(
        "/v1/knowledge/imports",
        json=payload,
        headers=_headers(csrf, "import-transaction-boundary"),
    )

    assert response.status_code == 200
    assert object_store.write_transaction_depths == [0]
    assert object_store.verify_transaction_depths
    assert all(depth == 0 for depth in object_store.verify_transaction_depths)
    with session_scope(session_factory) as session:
        artifacts = session.execute(
            text(
                """
                SELECT eo.object_uri, cv.text_artifact_uri, kv.markdown_uri,
                       eo.sha256, eo.byte_size
                FROM evidence_objects eo
                JOIN content_versions cv ON cv.evidence_object_id = eo.id
                JOIN knowledge_versions kv ON kv.content_version_id = cv.id
                """
            )
        ).mappings().one()
    object_store.verify_text_artifacts(
        StoredTextArtifacts(
            evidence_object_uri=str(artifacts["object_uri"]),
            text_artifact_uri=str(artifacts["text_artifact_uri"]),
            markdown_uri=str(artifacts["markdown_uri"]),
            sha256=str(artifacts["sha256"]),
            byte_size=int(artifacts["byte_size"]),
        )
    )


def test_knowledge_api_external_confirmation_records_user_decision_and_replays(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    text_value = "API 确认必须由认证用户发起，并绑定内容 hash。"
    repository = KnowledgeRepository()
    item = _external_candidate(text_value)
    artifacts = stored_text_artifacts(tmp_path, item.text)
    with session_scope(session_factory) as session:
        candidate = repository.create_external_candidate(
            session,
            item,
            stored_artifacts=artifacts,
        )

    payload = {
        "confirmation_request_id": candidate.confirmation_request_id,
        "expected_content_sha256": sha256_text(text_value),
    }
    first = client.post(
        f"/v1/knowledge/external-candidates/{candidate.knowledge_object_id}/confirmation",
        json=payload,
        headers=_headers(csrf, "confirm-1"),
    )
    replay = client.post(
        f"/v1/knowledge/external-candidates/{candidate.knowledge_object_id}/confirmation",
        json=payload,
        headers=_headers(csrf, "confirm-1"),
    )
    conflict = client.post(
        f"/v1/knowledge/external-candidates/{candidate.knowledge_object_id}/confirmation",
        json={**payload, "expected_content_sha256": sha256_text("other")},
        headers=_headers(csrf, "confirm-1"),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    with session_scope(session_factory) as session:
        decision = session.execute(
            text(
                """
                SELECT decided_by_user_id, final_value_json
                FROM knowledge_confirmation_decisions
                """
            )
        ).mappings().one()
    assert decision["decided_by_user_id"]
    assert text_value not in str(decision["final_value_json"])


def test_external_candidate_chunk_tamper_fails_confirmation(tmp_path: Path) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    text_value = "候选内容确认必须绑定到原始持久化文本。"
    item = _external_candidate(text_value)
    artifacts = stored_text_artifacts(tmp_path, item.text)

    with session_scope(session_factory) as session:
        candidate = repository.create_external_candidate(
            session,
            item,
            stored_artifacts=artifacts,
        )
        session.execute(
            text("UPDATE chunks SET raw_text = :raw_text WHERE id = :id"),
            {"id": candidate.chunk_id, "raw_text": "tampered"},
        )

        with pytest.raises(ValueError, match="chunk content hash mismatch"):
            repository.confirm_external_candidate(
                session,
                candidate.knowledge_object_id,
                confirmation_request_id=str(candidate.confirmation_request_id),
                expected_content_sha256=sha256_text(text_value),
                user_authority=KnowledgeUserAuthority("synthetic-user"),
            )


def test_formal_ingest_rejects_external_discovery_metadata_and_source_kind(
    tmp_path: Path,
) -> None:
    session_factory = _migrated_session_factory(tmp_path)
    repository = KnowledgeRepository()
    source_kind_item = TextEvidenceInput(
        title="伪装外部资料",
        primary_domain_id="technology.ai",
        text="不能靠 source_kind 让外部资料直接正式入库。",
        source_kind="web_snapshot",
    )
    metadata_item = TextEvidenceInput(
        title="伪装手动资料",
        primary_domain_id="technology.ai",
        text="外部发现元数据必须进入候选流。",
        source_kind="manual",
        source_metadata={"source_url": "https://example.invalid/manual"},
    )
    source_kind_artifacts = stored_text_artifacts(tmp_path, source_kind_item.text)
    metadata_artifacts = stored_text_artifacts(tmp_path, metadata_item.text)

    with session_scope(session_factory) as session:
        with pytest.raises(ValueError, match="user-provided source authority"):
            repository.ingest_text(
                session,
                source_kind_item,
                user_authority=KnowledgeUserAuthority("synthetic-user"),
                stored_artifacts=source_kind_artifacts,
            )
        with pytest.raises(ValueError, match="external discovery"):
            repository.ingest_text(
                session,
                metadata_item,
                user_authority=KnowledgeUserAuthority("synthetic-user"),
                stored_artifacts=metadata_artifacts,
            )
