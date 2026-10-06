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

from zhiheng.backup import collect_artifact_manifest, stage_backup, verify_backup_bundle
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.knowledge import KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore
from zhiheng.knowledge.service import KnowledgeIngestionService
from zhiheng.privacy.erase_journal import ExternalEraseJournal
from zhiheng.restore import prepare_restored_bundle


def _snapshot(tmp_path: Path) -> tuple[Path, Path, Path]:
    database = tmp_path / "live.sqlite"
    settings = Settings(database_url=f"sqlite:///{database}")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    root = tmp_path / "knowledge-object-store"
    store = LocalKnowledgeObjectStore(root)
    artifacts = store.write_text_artifacts("synthetic backup evidence")
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            KnowledgeIngestionService(settings).ingest_prepared_user_text(
                session,
                TextEvidenceInput(
                    title="synthetic title",
                    text="synthetic backup evidence",
                    primary_domain_id="technology.ai",
                ),
                user_authority=KnowledgeUserAuthority("synthetic-user"),
                stored_artifacts=artifacts,
            )
    finally:
        engine.dispose()
    snapshot = tmp_path / "snapshot.sqlite"
    source = sqlite3.connect(database)
    target = sqlite3.connect(snapshot)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return snapshot, root, Path(unquote(urlparse(artifacts.evidence_object_uri).path))


def test_manifest_covers_original_and_markdown_but_not_orphans(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    LocalKnowledgeObjectStore(root).write_text_artifacts("uncommitted orphan")
    manifest = collect_artifact_manifest(snapshot, root)
    assert len(manifest) == 3
    assert {item.relative_path.split("/")[0] for item in manifest} == {"evidence", "artifacts"}
    assert all(item.byte_size == len(b"synthetic backup evidence") for item in manifest)
    assert len({item.sha256 for item in manifest}) == 1


def test_manifest_rejects_corrupt_or_missing_referenced_evidence(tmp_path: Path) -> None:
    snapshot, root, evidence = _snapshot(tmp_path)
    evidence.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        collect_artifact_manifest(snapshot, root)
    evidence.unlink()
    with pytest.raises(FileNotFoundError):
        collect_artifact_manifest(snapshot, root)


def test_manifest_rejects_artifacts_outside_configured_root(tmp_path: Path) -> None:
    snapshot, _, _ = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        collect_artifact_manifest(snapshot, tmp_path / "different-root")


def test_staged_bundle_contains_verified_private_bytes(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["format_version"] == 1
    assert manifest["provider_secret_recovery"] == {
        "encrypted_records_in_database": True,
        "master_key_external_dependency": "native-keyring",
        "missing_master_key_behavior": "provider_reentry_required",
    }
    assert len(manifest["artifacts"]) == 3
    for artifact in manifest["artifacts"]:
        path = bundle / "objects" / artifact["relative_path"]
        assert path.read_bytes() == b"synthetic backup evidence"
        assert path.stat().st_mode & 0o777 == 0o600
    assert (bundle / "database.sqlite").stat().st_mode & 0o777 == 0o600
    assert len(verify_backup_bundle(bundle)) == 3
    with pytest.raises(FileExistsError):
        stage_backup(snapshot, root, bundle)


def test_real_restic_encrypts_and_restores_bundle(tmp_path: Path) -> None:
    binary = os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")
    if not binary:
        pytest.skip("restic executable required for encrypted backup rehearsal")
    snapshot, root, _ = _snapshot(tmp_path)
    env = {
        **os.environ,
        "RESTIC_REPOSITORY": str(tmp_path / "restic-repository"),
        "RESTIC_PASSWORD": "synthetic-restic-test-password",
        "ZHIHENG_RESTIC_BINARY": binary,
        "ZHIHENG_DATABASE_PATH": str(snapshot),
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(root),
    }
    subprocess.run([binary, "init"], env=env, capture_output=True, check=True)
    result = subprocess.run(
        [sys.executable, "scripts/backup_restic.py"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    snapshot_id = json.loads(result.stdout)["snapshot_id"]
    assert len(snapshot_id) == 64
    restored = tmp_path / "restored"
    subprocess.run(
        [binary, "restore", snapshot_id, "--target", str(restored), "--verify"],
        env=env,
        capture_output=True,
        check=True,
        timeout=60,
    )
    manifests = list(restored.rglob("manifest.json"))
    assert len(manifests) == 1
    assert len(verify_backup_bundle(manifests[0].parent)) == 3
    manifest = json.loads(manifests[0].read_text())
    for artifact in manifest["artifacts"]:
        assert (
            manifests[0].parent / "objects" / artifact["relative_path"]
        ).read_bytes() == b"synthetic backup evidence"
    subprocess.run([binary, "check"], env=env, capture_output=True, check=True)


@pytest.mark.parametrize("damage", ["database", "artifact", "extra", "escape"])
def test_restored_bundle_validation_fails_closed(tmp_path: Path, damage: str) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "database":
        (bundle / "database.sqlite").write_bytes(b"damaged")
    elif damage == "artifact":
        (bundle / "objects" / manifest["artifacts"][0]["relative_path"]).write_bytes(b"damaged")
    elif damage == "extra":
        (bundle / "unexpected.txt").write_text("unlisted")
    else:
        manifest["artifacts"][0]["relative_path"] = "../escape"
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify_backup_bundle(bundle)


def test_prepare_old_full_backup_replays_later_erase_only_in_staging(tmp_path: Path) -> None:
    snapshot, root, original_evidence = _snapshot(tmp_path)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    connection = sqlite3.connect(snapshot)
    try:
        object_id = connection.execute("SELECT id FROM knowledge_objects").fetchone()[0]
    finally:
        connection.close()
    journal = ExternalEraseJournal(tmp_path / "independent-journal.jsonl", "synthetic-secret")
    journal.append_intent(
        request_id="synthetic-post-backup-erase",
        target_type="knowledge_object",
        target_id=object_id,
    )
    journal_bytes = journal.path.read_bytes()
    remaining = prepare_restored_bundle(bundle, journal, project_root=Path.cwd())
    assert remaining == ()
    assert not any(path.is_file() for path in (bundle / "objects").rglob("*"))
    assert original_evidence.read_bytes() == b"synthetic backup evidence"
    assert b"synthetic backup evidence" not in (bundle / "database.sqlite").read_bytes()
    assert journal.path.read_bytes() == journal_bytes
    connection = sqlite3.connect(bundle / "database.sqlite")
    try:
        assert connection.execute("SELECT count(*) FROM serving_chunks").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM privacy_erase_requests").fetchone()[0] == (
            "completed"
        )
    finally:
        connection.close()


def test_prepare_refuses_missing_latest_journal_before_mutating_bundle(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    before = (bundle / "database.sqlite").read_bytes()
    journal = ExternalEraseJournal(tmp_path / "missing.jsonl", "synthetic-secret")
    with pytest.raises(ValueError, match="missing"):
        prepare_restored_bundle(bundle, journal, project_root=Path.cwd())
    assert (bundle / "database.sqlite").read_bytes() == before
