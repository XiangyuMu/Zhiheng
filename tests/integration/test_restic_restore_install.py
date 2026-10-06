from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from scripts import restore_restic
from zhiheng.api.main import create_app
from zhiheng.backup import BackupArtifact
from zhiheng.core.config import Settings
from zhiheng.db.maintenance import acquire_database_lock
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts
from zhiheng.privacy.erase_journal import ExternalEraseJournal
from zhiheng.retrieval.repository import CitationContextRepository, LexicalRetriever
from zhiheng.secrets import InMemoryMasterKeyBackend, ProviderSecretStore

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTIC_PASSWORD = "synthetic-restic-restore-password"
JOURNAL_SECRET = "test-secret-with-enough-length-for-hmac"
RESTORE_TEXT = "restic restore install must not revive erased private evidence bytes"


def _restic_binary() -> str:
    binary = os.environ.get("ZHIHENG_RESTIC_BINARY")
    if binary:
        if not Path(binary).is_file():
            pytest.fail(f"ZHIHENG_RESTIC_BINARY does not exist: {binary}")
        if not os.access(binary, os.X_OK):
            pytest.fail(f"ZHIHENG_RESTIC_BINARY is not executable: {binary}")
        return binary
    discovered = shutil.which("restic")
    if discovered:
        return discovered
    pytest.skip(
        "restic executable required for restic restore install tests; "
        "set ZHIHENG_RESTIC_BINARY or install restic on PATH"
    )


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _open_session_factory(db_path: Path) -> sessionmaker[Session]:
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _assert_current_schema(db_path: Path) -> None:
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    expected = set(script.get_heads())
    connection = sqlite3.connect(db_path)
    try:
        current = {
            str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version")
        }
    finally:
        connection.close()
    assert current == expected


def _ingest(session: Session, artifacts: StoredTextArtifacts) -> str:
    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title="restic restore install",
            primary_domain_id="privacy.erase",
            text=RESTORE_TEXT,
            source_metadata={"fixture": "synthetic"},
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    return str(ingested.knowledge_object_id)


def _make_backup(
    tmp_path: Path,
    binary: str,
    *,
    provider_secret_store: ProviderSecretStore | None = None,
) -> tuple[Path, str, str]:
    source_db = tmp_path / "source.db"
    source_objects = tmp_path / "source-objects"
    session_factory = _session_factory(source_db)
    artifacts = LocalKnowledgeObjectStore(source_objects).write_text_artifacts(RESTORE_TEXT)
    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(session, artifacts)
        # The fixture represents a completed indexing worker before the
        # snapshot is taken, so restored search observes the normal serving
        # qualification contract.
        mark_formal_knowledge_indexed(session, knowledge_object_id)
        if provider_secret_store is not None:
            session.execute(
                text(
                    """
                    INSERT INTO model_provider_configs (
                      id, provider_kind, display_name, enabled, policy_json, secret_ref,
                      model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                    ) VALUES (
                      'provider-restic', 'openai-compatible', 'Restic Provider', 1, '{}',
                      NULL, '[\"model-a\"]', 'https://models.example.test/v1',
                      'https://models.example.test', 'rev-1'
                    )
                    """
                )
            )
            stored = provider_secret_store.store(
                session,
                provider_id="provider-restic",
                secret=SecretStr("sk-restic-provider-secret"),
            )
            session.execute(
                text(
                    "UPDATE model_provider_configs SET secret_ref=:secret_ref "
                    "WHERE id='provider-restic'"
                ),
                {"secret_ref": stored.secret_ref},
            )
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
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
    result = subprocess.run(
        [sys.executable, "scripts/backup_restic.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    backup_payload = json.loads(result.stdout)
    assert backup_payload["format_version"] == 2
    if provider_secret_store is not None:
        assert backup_payload["provider_secret_recovery"]["encrypted_records_in_database"] is True
    snapshot_id = backup_payload["snapshot_id"]
    assert isinstance(snapshot_id, str)
    return repository, snapshot_id, knowledge_object_id


def _restore_env(
    repository: Path,
    binary: str,
    snapshot_id: str,
    target_db: Path,
    target_objects: Path,
    journal_path: Path,
) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": "src",
        "RESTIC_REPOSITORY": str(repository),
        "RESTIC_PASSWORD": RESTIC_PASSWORD,
        "ZHIHENG_RESTIC_BINARY": binary,
        "ZHIHENG_RESTIC_SNAPSHOT_ID": snapshot_id,
        "ZHIHENG_DATABASE_PATH": str(target_db),
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(target_objects),
        "ZHIHENG_ERASE_JOURNAL_PATH": str(journal_path),
        "ZHIHENG_SECRET_KEY": JOURNAL_SECRET,
    }


def _run_restore(env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/restore_restic.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _object_paths_from_db(db_path: Path) -> list[Path]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT eo.object_uri
            FROM evidence_objects eo
            UNION
            SELECT cv.text_artifact_uri
            FROM content_versions cv
            UNION
            SELECT kv.markdown_uri
            FROM knowledge_versions kv
            WHERE kv.markdown_uri IS NOT NULL
            ORDER BY 1
            """
        ).fetchall()
    finally:
        connection.close()
    paths = []
    for (uri,) in rows:
        parsed = urlparse(uri)
        assert parsed.scheme == "file"
        paths.append(Path(unquote(parsed.path)))
    return paths


def _empty_journal(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _digest(path: Path) -> str:
    import hashlib

    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def test_restic_restore_installs_clean_bundle_into_new_target(tmp_path: Path) -> None:
    binary = _restic_binary()
    repository, snapshot_id, _ = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)

    result = _run_restore(
        _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    )

    assert json.loads(result.stdout)["restored_snapshot_id"] == snapshot_id
    _assert_current_schema(target_db)
    with session_scope(_open_session_factory(target_db)) as session:
        assert session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one() == 1
        assert (
            session.execute(text("SELECT raw_text FROM serving_chunks")).scalar_one()
            == RESTORE_TEXT
        )
    object_paths = _object_paths_from_db(target_db)
    assert len(object_paths) == 3
    assert all(path.is_relative_to(target_objects) for path in object_paths)
    assert all(path.read_bytes() == RESTORE_TEXT.encode() for path in object_paths)


def test_restic_restore_preserves_provider_ciphertext_and_supports_reentry(
    tmp_path: Path,
) -> None:
    binary = _restic_binary()
    master_keys = InMemoryMasterKeyBackend()
    source_store = ProviderSecretStore(master_key_backend=master_keys)
    repository, snapshot_id, _ = _make_backup(
        tmp_path,
        binary,
        provider_secret_store=source_store,
    )
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)

    result = _run_restore(
        _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    )
    recovery = json.loads(result.stdout)["provider_secret_recovery"]
    assert recovery["encrypted_records_in_database"] is True
    with sqlite3.connect(target_db) as connection:
        row = connection.execute(
            "SELECT secret_version, algorithm, nonce_b64, ciphertext_b64, aad_json "
            "FROM provider_secret_records WHERE provider_id='provider-restic'"
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == "AES-256-GCM"
    assert all(isinstance(value, str) and value for value in row[2:])

    settings = Settings(environment="test", database_url=f"sqlite:///{target_db}")
    available = TestClient(
        create_app(
            settings,
            secret_store=ProviderSecretStore(master_key_backend=master_keys),
        )
    )
    bootstrap = available.post(
        "/auth/bootstrap",
        json={"username": "restore-owner", "password": "restore-owner-password"},
    )
    assert bootstrap.status_code == 200
    providers = available.get("/v1/model-config/providers")
    assert providers.status_code == 200
    assert providers.json()[0]["secret_status"] == "configured"

    unavailable = TestClient(
        create_app(
            settings,
            secret_store=ProviderSecretStore(
                master_key_backend=InMemoryMasterKeyBackend(available=False)
            ),
        )
    )
    login_unavailable = unavailable.post(
        "/auth/login",
        json={"username": "restore-owner", "password": "restore-owner-password"},
    )
    assert login_unavailable.status_code == 200
    unavailable_listing = unavailable.get("/v1/model-config/providers")
    assert unavailable_listing.status_code == 200
    assert unavailable_listing.json()[0]["secret_status"] == "unavailable"

    reentry_backend = InMemoryMasterKeyBackend()
    reentry = TestClient(
        create_app(settings, secret_store=ProviderSecretStore(master_key_backend=reentry_backend))
    )
    login_reentry = reentry.post(
        "/auth/login",
        json={"username": "restore-owner", "password": "restore-owner-password"},
    )
    assert login_reentry.status_code == 200
    csrf = login_reentry.json()["csrf_token"]
    current = reentry.get("/v1/model-config/providers").json()[0]
    replaced = reentry.patch(
        "/v1/model-config/providers/provider-restic",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": current["etag"],
            "Idempotency-Key": "restic-provider-reentry",
        },
        json={"api_key": "sk-restic-reentered-secret"},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["secret_status"] == "configured"


def test_restic_restore_supports_real_search_and_original_source_resolution(
    tmp_path: Path,
) -> None:
    binary = _restic_binary()
    repository, snapshot_id, knowledge_object_id = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)

    _run_restore(
        _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    )

    upgraded = subprocess.run(
        [sys.executable, "scripts/upgrade_database.py", str(target_db)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    _assert_current_schema(target_db)
    with session_scope(_open_session_factory(target_db)) as session:
        hits = LexicalRetriever().search(session, "restore install")
        assert [hit.source_id for hit in hits] == [knowledge_object_id]
        hit = hits[0]
        chunk = CitationContextRepository().get_chunk(
            session,
            source_type=hit.source_type,
            source_id=hit.source_id,
            source_version_id=hit.source_version_id,
            chunk_id=hit.chunk_id,
        )
        assert chunk is not None
        assert chunk["text"] == RESTORE_TEXT

        object_uri = session.execute(
            text(
                """
                SELECT eo.object_uri
                FROM evidence_objects eo
                JOIN content_versions cv ON cv.evidence_object_id = eo.id
                JOIN knowledge_versions kv ON kv.content_version_id = cv.id
                WHERE kv.knowledge_object_id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        ).scalar_one()

    assert LocalKnowledgeObjectStore(target_objects).read_bytes(str(object_uri)) == (
        RESTORE_TEXT.encode()
    )


def test_restic_restore_replays_later_external_erase_against_final_object_root(
    tmp_path: Path,
) -> None:
    binary = _restic_binary()
    repository, snapshot_id, knowledge_object_id = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)
    env = _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    _run_restore(env)
    restored_object_paths = _object_paths_from_db(target_db)
    assert all(path.exists() for path in restored_object_paths)

    ExternalEraseJournal(journal_path, JOURNAL_SECRET).append_intent(
        request_id="post-backup-erase",
        target_type="knowledge_object",
        target_id=knowledge_object_id,
    )
    _run_restore(env)

    assert all(not path.exists() for path in restored_object_paths)
    assert RESTORE_TEXT.encode() not in target_db.read_bytes()
    with session_scope(_open_session_factory(target_db)) as session:
        status = session.execute(
            text("SELECT lifecycle_status FROM knowledge_objects WHERE id = :id"),
            {"id": knowledge_object_id},
        ).scalar_one()
        assert status == "privacy_erased"
        assert session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one() == 0


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_restic_restore_refuses_missing_or_tampered_journal_without_visible_change(
    tmp_path: Path,
    damage: str,
) -> None:
    binary = _restic_binary()
    repository, snapshot_id, knowledge_object_id = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)
    env = _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    _run_restore(env)
    before_db = target_db.read_bytes()
    before_paths = {path: path.read_bytes() for path in _object_paths_from_db(target_db)}

    if damage == "missing":
        journal_path.unlink()
    else:
        ExternalEraseJournal(journal_path, JOURNAL_SECRET).append_intent(
            request_id="tampered-erase",
            target_type="knowledge_object",
            target_id=knowledge_object_id,
        )
        journal_path.write_text(
            journal_path.read_text(encoding="utf-8").replace(knowledge_object_id, "changed-id"),
            encoding="utf-8",
        )

    result = _run_restore(env, check=False)

    assert result.returncode != 0
    assert target_db.read_bytes() == before_db
    assert {path: path.read_bytes() for path in before_paths} == before_paths


def test_object_install_refuses_symlink_escape_without_writing_outside(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    staged_file = staged / "evidence" / "item.bin"
    staged_file.parent.mkdir(parents=True)
    staged_file.write_bytes(b"private")
    object_root = tmp_path / "objects"
    outside = tmp_path / "outside"
    outside.mkdir()
    object_root.mkdir()
    (object_root / "evidence").symlink_to(outside, target_is_directory=True)
    artifact = BackupArtifact("evidence/item.bin", _digest(staged_file), staged_file.stat().st_size)

    with pytest.raises(RuntimeError, match="symlink"):
        restore_restic._install_surviving_objects(staged, object_root, (artifact,))

    assert not (outside / "item.bin").exists()


def test_object_install_refuses_conflicting_existing_bytes_without_overwrite(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    staged_file = staged / "evidence" / "item.bin"
    staged_file.parent.mkdir(parents=True)
    staged_file.write_bytes(b"restored")
    object_root = tmp_path / "objects"
    existing = object_root / "evidence" / "item.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    artifact = BackupArtifact("evidence/item.bin", _digest(staged_file), staged_file.stat().st_size)

    with pytest.raises(RuntimeError, match="conflicting object"):
        restore_restic._install_surviving_objects(staged, object_root, (artifact,))

    assert existing.read_bytes() == b"existing"


def test_object_install_publishes_multiple_files_and_fsyncs_touched_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "staged"
    first = staged / "evidence" / "one.bin"
    second = staged / "evidence" / "two.bin"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    object_root = tmp_path / "objects"
    artifacts = (
        BackupArtifact("evidence/one.bin", _digest(first), first.stat().st_size),
        BackupArtifact("evidence/two.bin", _digest(second), second.stat().st_size),
    )
    fsynced: list[Path] = []
    monkeypatch.setattr(restore_restic, "_fsync_directory", lambda path: fsynced.append(path))

    restore_restic._install_surviving_objects(staged, object_root, artifacts)

    assert (object_root / "evidence" / "one.bin").read_bytes() == b"one"
    assert (object_root / "evidence" / "two.bin").read_bytes() == b"two"
    assert object_root in fsynced
    assert object_root / "evidence" in fsynced


def test_restic_restore_rejects_object_root_containing_database_before_change(
    tmp_path: Path,
) -> None:
    binary = _restic_binary()
    repository, snapshot_id, _ = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)
    env = _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    _run_restore(env)
    before_db = target_db.read_bytes()
    before_paths = {path: path.read_bytes() for path in _object_paths_from_db(target_db)}
    invalid_env = _restore_env(
        repository,
        binary,
        snapshot_id,
        target_db,
        target_db.parent,
        journal_path,
    )

    result = _run_restore(invalid_env, check=False)

    assert result.returncode != 0
    assert "object root must not contain" in result.stderr
    assert target_db.read_bytes() == before_db
    assert {path: path.read_bytes() for path in before_paths} == before_paths


def test_restic_restore_refuses_while_serving_lock_is_active(tmp_path: Path) -> None:
    binary = _restic_binary()
    repository, snapshot_id, _ = _make_backup(tmp_path, binary)
    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    _empty_journal(journal_path)
    env = _restore_env(repository, binary, snapshot_id, target_db, target_objects, journal_path)
    _run_restore(env)
    before_db = target_db.read_bytes()
    descriptor = acquire_database_lock(str(target_db), exclusive=False)
    try:
        result = _run_restore(env, check=False)
    finally:
        os.close(descriptor)

    assert result.returncode != 0
    assert target_db.read_bytes() == before_db


@pytest.mark.parametrize(
    ("behavior", "code", "timeout"),
    [
        ("import time; time.sleep(60)", "RESTIC_RESTORE_TIMEOUT", "0.2"),
        ("sys.exit(11)", "RESTIC_REPOSITORY_LOCKED", "10"),
        ("sys.exit(12)", "RESTIC_AUTH_FAILED", "10"),
        ("sys.exit(1)", "RESTIC_RESTORE_FAILED", "10"),
        ("sys.exit(0)", "RESTIC_RESTORE_CONFIG_INVALID", "nan"),
        ("sys.exit(0)", "RESTIC_RESTORE_CONFIG_INVALID", "0"),
        ("sys.exit(0)", "RESTIC_RESTORE_CONFIG_INVALID", "invalid"),
    ],
)
def test_restore_cli_reports_safe_failure_and_cleans_staging(
    tmp_path: Path, behavior: str, code: str, timeout: str
) -> None:
    binary = tmp_path / "fake-restic"
    binary.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({RESTIC_PASSWORD!r}, file=sys.stderr, flush=True)\n{behavior}\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    database = tmp_path / "target.db"
    database.write_bytes(b"original database")
    journal = tmp_path / "journal.jsonl"
    _empty_journal(journal)
    env = _restore_env(
        tmp_path / "repository",
        str(binary),
        "a" * 64,
        database,
        tmp_path / "objects",
        journal,
    )
    env["ZHIHENG_RESTIC_RESTORE_TIMEOUT_SECONDS"] = timeout
    result = _run_restore(env, check=False)
    assert result.returncode != 0
    diagnostic = json.loads(result.stderr)
    assert diagnostic["error_code"] == code
    assert diagnostic["reason"]
    assert diagnostic["elapsed_seconds"] >= 0
    assert RESTIC_PASSWORD not in result.stderr + result.stdout
    assert database.read_bytes() == b"original database"
    assert not list(tmp_path.glob("target.db.restic-restore.*"))


def test_restore_timeout_terminates_transport_children(tmp_path: Path) -> None:
    import time

    marker = tmp_path / "child-survived"
    pid_file = tmp_path / "restic.pid"
    binary = tmp_path / "fake-restic"
    child_code = (
        f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).touch(); "
        "time.sleep(60)"
    )
    binary.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    journal = tmp_path / "journal.jsonl"
    _empty_journal(journal)
    env = _restore_env(
        tmp_path / "repository",
        str(binary),
        "a" * 64,
        tmp_path / "target.db",
        tmp_path / "objects",
        journal,
    )
    env["ZHIHENG_RESTIC_RESTORE_TIMEOUT_SECONDS"] = "1.0"
    result = _run_restore(env, check=False)
    assert json.loads(result.stderr)["error_code"] == "RESTIC_RESTORE_TIMEOUT"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    time.sleep(1.6)
    assert not marker.exists()
    assert not list(tmp_path.glob("target.db.restic-restore.*"))


@pytest.mark.parametrize("termination", [15, 2])
def test_restore_signal_reaps_restic_and_transport_children(
    tmp_path: Path, termination: int
) -> None:
    import signal
    import time
    from contextlib import suppress

    binary = tmp_path / "fake-restic"
    pid_file = tmp_path / "restic.pid"
    marker = tmp_path / "transport-survived"
    child_code = (
        f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch(); "
        "time.sleep(60)"
    )
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', "
        f"{child_code!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    database = tmp_path / "target.db"
    database.write_bytes(b"unchanged")
    journal = tmp_path / "journal.jsonl"
    _empty_journal(journal)
    env = _restore_env(
        tmp_path / "repository",
        str(binary),
        "a" * 64,
        database,
        tmp_path / "objects",
        journal,
    )
    with subprocess.Popen(
        [sys.executable, "scripts/restore_restic.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.02)
            assert pid_file.exists(), "restic did not start"
            process.send_signal(termination)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 128 + termination
            assert RESTIC_PASSWORD not in stdout + stderr
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            time.sleep(2.1)
            assert not marker.exists()
            assert database.read_bytes() == b"unchanged"
            assert not list(tmp_path.glob("target.db.restic-restore.*"))
        finally:
            # Never leave processes from a failing regression behind.
            if pid_file.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait()
