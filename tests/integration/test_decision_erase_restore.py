from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _headers
from tests.integration.test_restic_restore_install import (
    JOURNAL_SECRET,
    REPO_ROOT,
    RESTIC_PASSWORD,
    _empty_journal,
    _open_session_factory,
    _restic_binary,
    _restore_env,
    _run_restore,
)
from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.decisions import DecisionSupportService
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.privacy.erase_journal import ExternalEraseJournal

TARGET_TEXT = "decision restore target evidence must disappear after privacy erase"
ANALYZE_PAYLOAD = {
    "problem": "是否保留这项学习计划？",
    "evidence_query": "decision restore target evidence",
    "options": [{"label": "keep", "description": "保留学习计划"}],
    "formal_goal_refs": ["goal.finance"],
}


def _source_factory(db_path: Path, object_root: Path) -> sessionmaker[Session]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    object_root.mkdir(parents=True, exist_ok=True)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        knowledge_object_store_path=str(object_root),
        secret_key=JOURNAL_SECRET,
    )
    return create_session_factory(create_sqlite_engine(settings))


def _backup_source(
    tmp_path: Path,
    binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, Path, str, str, str, str]:
    source_db = tmp_path / "source" / "zhiheng.db"
    source_objects = tmp_path / "source" / "objects"
    journal_path = tmp_path / "latest-erase-journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    _empty_journal(journal_path)
    monkeypatch.setenv("ZHIHENG_ERASE_JOURNAL_PATH", str(journal_path))
    monkeypatch.setenv("ZHIHENG_SECRET_KEY", JOURNAL_SECRET)
    factory = _source_factory(source_db, source_objects)
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{source_db}",
        knowledge_object_store_path=str(source_objects),
        secret_key=JOURNAL_SECRET,
    )
    with TestClient(create_app(settings)) as client:
        csrf = client.post(
            "/auth/bootstrap",
            json={"username": "solo_user", "password": "correct horse battery staple"},
        ).json()["csrf_token"]
        artifacts = LocalKnowledgeObjectStore(source_objects).write_text_artifacts(TARGET_TEXT)
        with session_scope(factory) as session:
            goal = MemoryRepository().commit_explicit_memory(
                session,
                MemoryValue(
                    memory_type="goal",
                    state_key="goal.finance",
                    value={"text": "learn quantitative finance"},
                ),
                operation_key="seed-goal",
            )
            knowledge = KnowledgeRepository().ingest_text(
                session,
                TextEvidenceInput(
                    title="decision restore target",
                    primary_domain_id="privacy.erase",
                    text=TARGET_TEXT,
                    source_metadata={"fixture": "issue-25"},
                ),
                user_authority=KnowledgeUserAuthority("synthetic-test-user"),
                stored_artifacts=artifacts,
            )
            mark_formal_knowledge_indexed(session, knowledge.knowledge_object_id)
        analyzed = client.post(
            "/v1/decisions/analyze",
            json=ANALYZE_PAYLOAD,
            headers=_headers(csrf, "issue-25-analyze"),
        )
        assert analyzed.status_code == 200, analyzed.text
        run_id = analyzed.json()["run_id"]

        # These rows share bytes with the bound source but have distinct IDs.
        other_artifacts = LocalKnowledgeObjectStore(source_objects).write_text_artifacts(
            TARGET_TEXT
        )
        with session_scope(factory) as session:
            other = KnowledgeRepository().ingest_text(
                session,
                TextEvidenceInput(
                    title="unrelated same-content source",
                    primary_domain_id="privacy.erase",
                    text=TARGET_TEXT,
                    source_metadata={"fixture": "issue-25-unrelated"},
                ),
                user_authority=KnowledgeUserAuthority("synthetic-test-user"),
                stored_artifacts=other_artifacts,
            )
            mark_formal_knowledge_indexed(session, other.knowledge_object_id)
            session.execute(
                text(
                    """
                    INSERT INTO decision_support_runs
                      (id, prompt_hash, decision_type, template_id, status,
                       recommendation_json, review_json, external_action_count)
                    VALUES
                      ('legacy-decision-exact-id', 'legacy-decision-exact-id-hash',
                       'compare', 'g005.default', 'completed', :exact_id, :exact_id, 0),
                      ('legacy-decision-same-text', 'legacy-decision-same-text-hash',
                       'compare', 'g005.default', 'completed', :same_text, :same_text, 0),
                      ('legacy-decision-paraphrase', 'legacy-decision-paraphrase-hash',
                       'compare', 'g005.default', 'completed', :paraphrase, :paraphrase, 0),
                      ('legacy-decision-corrupt', 'legacy-decision-corrupt-hash',
                       'compare', 'g005.default', 'completed', '{malformed', '{malformed', 0),
                      ('bound-decision-corrupt', 'bound-decision-corrupt-hash',
                       'compare', 'g005.default', 'completed', '{malformed', '{malformed', 0),
                      ('bound-knowledge-decision-corrupt', 'bound-knowledge-decision-corrupt-hash',
                       'compare', 'g005.default', 'completed', '{malformed', '{malformed', 0)
                    """
                ),
                {
                    "exact_id": json.dumps({"answer": str(goal.formal_memory_id)}),
                    "same_text": json.dumps({"answer": "learn quantitative finance"}),
                    "paraphrase": json.dumps({"answer": "keep studying finance"}),
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO decision_run_sources (run_id, source_type, source_id)
                    VALUES
                      ('bound-decision-corrupt', 'formal_memory', :goal_id),
                      ('bound-knowledge-decision-corrupt', 'knowledge_object', :knowledge_id)
                    """
                ),
                {"goal_id": goal.formal_memory_id, "knowledge_id": knowledge.knowledge_object_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO memory_operation_receipts
                      (id, operation_key, operation_type, request_hash, status, result_json)
                    VALUES
                      ('legacy-paraphrase', 'legacy-paraphrase', 'analyze_decision',
                       'legacy-paraphrase-hash', 'completed',
                       :result_json),
                      ('legacy-collision', 'legacy-collision', 'analyze_decision',
                       'legacy-collision-hash', 'completed',
                       :collision_json),
                      ('legacy-corrupt', 'legacy-corrupt', 'analyze_decision',
                       'legacy-corrupt-hash', 'completed', '{malformed'),
                      ('bound-corrupt', 'bound-corrupt', 'analyze_decision',
                       'bound-corrupt-hash', 'completed', '{malformed')
                    """
                ),
                {
                    "result_json": json.dumps(
                        {"response": {"answer": "keep learning quantitative finance"}}
                    ),
                    "collision_json": json.dumps(
                        {"response": {"answer": str(goal.formal_memory_id)}}
                    ),
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO memory_operation_receipt_sources
                      (receipt_id, source_type, source_id)
                    VALUES ('bound-corrupt', 'formal_memory', :goal_id)
                    """
                ),
                {"goal_id": goal.formal_memory_id},
            )
            session.commit()

    engine = factory.kw["bind"]
    engine.dispose()
    repository = tmp_path / "restic-repository"
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "RESTIC_REPOSITORY": str(repository),
        "RESTIC_PASSWORD": RESTIC_PASSWORD,
        "ZHIHENG_RESTIC_BINARY": binary,
        "ZHIHENG_DATABASE_PATH": str(source_db),
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(source_objects),
    }
    subprocess.run([binary, "init"], cwd=REPO_ROOT, env=env, capture_output=True, check=True)
    backup = subprocess.run(
        [sys.executable, "scripts/backup_restic.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    snapshot_id = str(json.loads(backup.stdout)["snapshot_id"])
    return (
        repository,
        snapshot_id,
        journal_path,
        str(goal.formal_memory_id),
        str(knowledge.knowledge_object_id),
        str(other.knowledge_object_id),
        run_id,
    )


def test_restic_restore_replays_decision_erase_for_formal_and_knowledge_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _restic_binary()
    (
        repository,
        snapshot_id,
        journal_path,
        goal_id,
        knowledge_id,
        other_knowledge_id,
        run_id,
    ) = _backup_source(tmp_path, binary, monkeypatch)
    journal = ExternalEraseJournal(journal_path, JOURNAL_SECRET)
    journal.append_intent(
        request_id="issue-25-formal-erase",
        target_type="formal_memory",
        target_id=goal_id,
    )
    journal.append_intent(
        request_id="issue-25-knowledge-erase",
        target_type="knowledge_object",
        target_id=knowledge_id,
    )

    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    env = _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    _run_restore(env)

    def assert_erased() -> None:
        factory = _open_session_factory(target_db)
        with session_scope(factory) as session:
            assert DecisionSupportService().get_analysis(session, run_id) is None
            hits = KnowledgeRepository().search_formal_fts(session, "decision restore target")
            assert {hit.source_id for hit in hits} == {other_knowledge_id}
            rows = {
                str(row["id"]): (str(row["status"]), str(row["result_json"]))
                for row in session.execute(
                    text(
                        """
                        SELECT id, status, result_json FROM memory_operation_receipts
                        WHERE id IN (
                          'legacy-paraphrase', 'legacy-collision', 'legacy-corrupt', 'bound-corrupt'
                        )
                        """
                    )
                ).mappings()
            }
            assert rows["bound-corrupt"] == ("privacy_erased", "{}")
            assert rows["legacy-paraphrase"][0] == "completed"
            assert rows["legacy-collision"][0] == "completed"
            assert rows["legacy-corrupt"][0] == "completed"
            decision_rows = {
                str(row["id"]): (
                    str(row["status"]),
                    str(row["recommendation_json"]),
                    str(row["review_json"]),
                )
                for row in session.execute(
                    text(
                        """
                        SELECT id, status, recommendation_json, review_json
                        FROM decision_support_runs
                        WHERE id IN (
                          'legacy-decision-exact-id',
                          'legacy-decision-same-text',
                          'legacy-decision-paraphrase',
                          'legacy-decision-corrupt',
                          'bound-decision-corrupt',
                          'bound-knowledge-decision-corrupt'
                        )
                        """
                    )
                ).mappings()
            }
            assert decision_rows["bound-decision-corrupt"] == (
                "privacy_erased",
                "{}",
                "{}",
            )
            assert decision_rows["bound-knowledge-decision-corrupt"] == (
                "privacy_erased",
                "{}",
                "{}",
            )
            for legacy_run_id in (
                "legacy-decision-exact-id",
                "legacy-decision-same-text",
                "legacy-decision-paraphrase",
                "legacy-decision-corrupt",
            ):
                assert decision_rows[legacy_run_id][0] == "completed"
            assert (
                session.execute(
                    text("SELECT status FROM formal_memories WHERE id=:id"), {"id": goal_id}
                ).scalar_one()
                == "privacy_erased"
            )
            assert (
                session.execute(
                    text("SELECT lifecycle_status FROM knowledge_objects WHERE id=:id"),
                    {"id": knowledge_id},
                ).scalar_one()
                == "privacy_erased"
            )
            assert (
                session.execute(
                    text("SELECT lifecycle_status FROM knowledge_objects WHERE id=:id"),
                    {"id": other_knowledge_id},
                ).scalar_one()
                == "formal_current"
            )
            unresolved = {
                str(row[0])
                for row in session.execute(
                    text(
                        """
                        SELECT derived_id FROM privacy_erase_unresolved_derived
                        WHERE erase_request_id IN (
                          'issue-25-formal-erase', 'issue-25-knowledge-erase'
                        )
                        """
                    )
                )
            }
            assert {"legacy-collision", "legacy-corrupt"} <= unresolved
            formal_unresolved = {
                str(row[0])
                for row in session.execute(
                    text(
                        """
                        SELECT derived_id FROM privacy_erase_unresolved_derived
                        WHERE erase_request_id = 'issue-25-formal-erase'
                          AND derived_type = 'decision_support_run'
                        """
                    )
                )
            }
            assert {
                "legacy-decision-exact-id",
                "legacy-decision-corrupt",
            } <= formal_unresolved
            assert {
                "legacy-decision-exact-id",
                "legacy-decision-same-text",
                "legacy-decision-corrupt",
            } <= unresolved
        factory.kw["bind"].dispose()

    assert_erased()
    object_factory = _open_session_factory(target_db)
    with session_scope(object_factory) as session:
        object_rows = session.execute(
            text(
                """
                SELECT ko.id, eo.object_uri
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.knowledge_object_id = ko.id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE ko.id IN (:erased_id, :other_id)
                """
            ),
            {"erased_id": knowledge_id, "other_id": other_knowledge_id},
        ).all()
    object_factory.kw["bind"].dispose()
    object_paths = {
        str(source_id): Path(unquote(urlparse(str(uri)).path))
        for source_id, uri in object_rows
    }
    assert not object_paths[knowledge_id].exists()
    assert object_paths[other_knowledge_id].read_bytes() == TARGET_TEXT.encode()

    _run_restore(env)
    assert_erased()

    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{target_db}",
        knowledge_object_store_path=str(target_objects),
        secret_key=JOURNAL_SECRET,
    )
    with TestClient(create_app(settings)) as client:
        login = client.post(
            "/auth/login",
            json={"username": "solo_user", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        replay = client.post(
            "/v1/decisions/analyze",
            json=ANALYZE_PAYLOAD,
            headers=_headers(csrf, "issue-25-analyze"),
        )
        assert replay.status_code == 409
        save = client.post(
            f"/v1/decisions/{run_id}/save",
            json={},
            headers=_headers(csrf, "issue-25-save"),
        )
        assert save.status_code == 404
