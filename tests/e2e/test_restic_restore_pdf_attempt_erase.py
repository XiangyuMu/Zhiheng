"""Back up, restore and privacy-erase a PDF whose parse attempt no text reference links to."""

from __future__ import annotations

import hashlib
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

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTIC_PASSWORD = "synthetic-restic-pdf-attempt-password"
JOURNAL_SECRET = "test-secret-with-enough-length-for-hmac"


def _restic_binary() -> str:
    binary = os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")
    if not binary:
        pytest.skip("restic executable required; set ZHIHENG_RESTIC_BINARY or put restic on PATH")
    return binary


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _add_soft_deleted_legacy_pdf(database: Path, objects: Path) -> str:
    """A PDF published before 5ffdf79 (empty text ref) whose later attempt kept a manifest."""
    pdf = _write(objects / "evidence" / "legacy.pdf", b"%PDF-1.7 synthetic legacy")
    manifest = _write(objects / "manifests" / "later.json", b'{"blocks": ["later attempt"]}')
    render = _write(objects / "renders" / "later-page-1.png", b"synthetic render")
    image = _write(objects / "images" / "later-image-1.png", b"synthetic image")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO evidence_objects (id, object_uri, sha256, media_type, byte_size, "
            "source_kind, status) VALUES ('pdf-evidence', ?, ?, 'application/pdf', ?, "
            "'upload', 'active')",
            (pdf.as_uri(), _digest(pdf), pdf.stat().st_size),
        )
        connection.execute(
            "INSERT INTO pdf_tasks (id, evidence_object_id, options_hash, idempotency_key, "
            "state) VALUES ('pdf-task', 'pdf-evidence', 'options', 'pdf-task', 'parsed')"
        )
        connection.execute(
            "INSERT INTO pdf_parse_attempts (id, task_id, evidence_object_id, backend, "
            "attempt_no, status, manifest_uri, manifest_sha256) VALUES ('pdf-attempt', "
            "'pdf-task', 'pdf-evidence', 'mineru', 1, 'succeeded', ?, ?)",
            (manifest.as_uri(), _digest(manifest)),
        )
        connection.execute(
            "INSERT INTO pdf_pages (id, attempt_id, page_no, page_width, page_height, "
            "crop_box_json, render_uri, render_sha256) "
            "VALUES ('pdf-page', 'pdf-attempt', 1, 612, 792, '[]', ?, ?)",
            (render.as_uri(), _digest(render)),
        )
        connection.execute(
            "INSERT INTO pdf_images (id, attempt_id, page_id, artifact_uri, sha256, media_type, "
            "bbox_json) VALUES ('pdf-image', 'pdf-attempt', 'pdf-page', ?, ?, 'image/png', '[]')",
            (image.as_uri(), _digest(image)),
        )
        connection.execute(
            "INSERT INTO content_versions (id, evidence_object_id, version_no, processor_name, "
            "processor_version, text_artifact_uri, content_sha256, status) VALUES "
            "('pdf-content', 'pdf-evidence', 1, 'pdf-parser', 'manifest-v1', '', ?, 'active')",
            (hashlib.sha256(b"legacy extracted text").hexdigest(),),
        )
        connection.execute(
            "INSERT INTO knowledge_objects (id, primary_domain_id, title, object_kind, "
            "lifecycle_status, visibility_scope, sensitivity_level) VALUES ('pdf-object', "
            "'technology.ai', 'legacy pdf', 'document', 'soft_deleted', 'private', 'normal')"
        )
        connection.execute(
            "INSERT INTO knowledge_versions (id, knowledge_object_id, version_no, "
            "content_version_id, source_quality) "
            "VALUES ('pdf-version', 'pdf-object', 1, 'pdf-content', 'parsed')"
        )
        connection.execute(
            "UPDATE knowledge_objects SET current_version_id='pdf-version' WHERE id='pdf-object'"
        )
    return "pdf-object"


def _pdf_attempt_paths(database: Path) -> list[Path]:
    with sqlite3.connect(database) as connection:
        uris = connection.execute(
            "SELECT a.manifest_uri, p.render_uri, i.artifact_uri FROM pdf_parse_attempts a "
            "JOIN pdf_pages p ON p.attempt_id = a.id JOIN pdf_images i ON i.attempt_id = a.id"
        ).fetchone()
    return [Path(unquote(urlparse(uri).path)) for uri in uris]


def test_restored_instance_erases_pdf_attempt_artifacts_no_text_reference_links_to(
    tmp_path: Path,
) -> None:
    binary = _restic_binary()
    source_db = tmp_path / "source" / "zhiheng.db"
    source_objects = tmp_path / "source" / "objects"
    source_db.parent.mkdir()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{source_db}")
    command.upgrade(config, "head")
    knowledge_object_id = _add_soft_deleted_legacy_pdf(source_db, source_objects)

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
    snapshot_id = json.loads(backup.stdout)["snapshot_id"]

    target_db = tmp_path / "target" / "zhiheng.db"
    target_objects = tmp_path / "target" / "objects"
    journal_path = tmp_path / "target" / "erase-journal.jsonl"
    journal_path.parent.mkdir()
    journal_path.touch()
    subprocess.run(
        [sys.executable, "scripts/restore_restic.py"],
        cwd=REPO_ROOT,
        env={
            **env,
            "ZHIHENG_RESTIC_SNAPSHOT_ID": snapshot_id,
            "ZHIHENG_DATABASE_PATH": str(target_db),
            "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(target_objects),
            "ZHIHENG_ERASE_JOURNAL_PATH": str(journal_path),
            "ZHIHENG_SECRET_KEY": JOURNAL_SECRET,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    restored = _pdf_attempt_paths(target_db)
    assert all(path.is_relative_to(target_objects.resolve()) for path in restored)
    assert [path.read_bytes() for path in restored] == [
        b'{"blocks": ["later attempt"]}',
        b"synthetic render",
        b"synthetic image",
    ]

    settings = Settings(environment="test", database_url=f"sqlite:///{target_db}")
    engine = create_sqlite_engine(settings)
    service = PrivacyEraseService(
        ExternalEraseJournal(journal_path, JOURNAL_SECRET), object_store_root=target_objects
    )
    try:
        with session_scope(create_session_factory(engine)) as session:
            intent = service.request_erase(
                session,
                target_type="knowledge_object",
                target_id=knowledge_object_id,
                requester="user",
                reason="synthetic restored erase",
            )
            service.execute_knowledge_erase(
                session, request_id=intent.request_id, knowledge_object_id=knowledge_object_id
            )
    finally:
        engine.dispose()

    assert not any(path.exists() for path in restored)
    assert not (target_objects / "evidence" / "legacy.pdf").exists()
    with sqlite3.connect(target_db) as connection:
        assert connection.execute(
            "SELECT lifecycle_status FROM knowledge_objects WHERE id=?", (knowledge_object_id,)
        ).fetchone() == ("privacy_erased",)
