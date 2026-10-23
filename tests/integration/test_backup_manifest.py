from __future__ import annotations

import base64
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

from zhiheng.backup import collect_artifact_manifest, stage_backup, verify_backup_bundle
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.knowledge import KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore
from zhiheng.knowledge.service import KnowledgeIngestionService
from zhiheng.privacy.erase_journal import ExternalEraseJournal
from zhiheng.restore import prepare_restored_bundle, relocate_object_references


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


def _add_pdf(snapshot: Path, root: Path, name: str, *, manifest: bool) -> str:
    """Add a PDF knowledge item as published before (empty text ref) or after 5ffdf79."""
    pdf = root / "evidence" / f"{name}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(f"%PDF-1.7 {name}".encode())
    body_sha = hashlib.sha256(f"{name} extracted text".encode()).hexdigest()
    text_uri = ""
    with sqlite3.connect(snapshot) as connection:
        connection.execute(
            "INSERT INTO evidence_objects (id, object_uri, sha256, media_type, byte_size, "
            "source_kind, status) VALUES (?, ?, ?, 'application/pdf', ?, 'upload', 'active')",
            (
                f"{name}-evidence",
                pdf.as_uri(),
                hashlib.sha256(pdf.read_bytes()).hexdigest(),
                pdf.stat().st_size,
            ),
        )
        if manifest:
            manifest_path = root / "pdf-attempts" / name / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"blocks": [f"{name} extracted text"]}))
            text_uri = manifest_path.as_uri()
            connection.execute(
                "INSERT INTO pdf_tasks (id, evidence_object_id, options_hash, idempotency_key, "
                "state) VALUES (?, ?, 'options', ?, 'parsed')",
                (f"{name}-task", f"{name}-evidence", f"{name}-task"),
            )
            connection.execute(
                "INSERT INTO pdf_parse_attempts (id, task_id, evidence_object_id, backend, "
                "attempt_no, status, manifest_uri, manifest_sha256) "
                "VALUES (?, ?, ?, 'mineru', 1, 'succeeded', ?, ?)",
                (
                    f"{name}-attempt",
                    f"{name}-task",
                    f"{name}-evidence",
                    text_uri,
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                ),
            )
        connection.execute(
            "INSERT INTO content_versions (id, evidence_object_id, version_no, processor_name, "
            "processor_version, text_artifact_uri, content_sha256, status) "
            "VALUES (?, ?, 1, 'pdf-parser', 'manifest-v1', ?, ?, 'active')",
            (f"{name}-content", f"{name}-evidence", text_uri, body_sha),
        )
        connection.execute(
            "INSERT INTO knowledge_objects (id, primary_domain_id, title, object_kind, "
            "lifecycle_status, visibility_scope, sensitivity_level) "
            "VALUES (?, 'technology.ai', ?, 'document', 'formal_current', 'private', 'normal')",
            (f"{name}-object", name),
        )
        connection.execute(
            "INSERT INTO knowledge_versions (id, knowledge_object_id, version_no, "
            "content_version_id, source_quality) VALUES (?, ?, 1, ?, 'parsed')",
            (f"{name}-version", f"{name}-object", f"{name}-content"),
        )
        connection.execute(
            "UPDATE knowledge_objects SET current_version_id=? WHERE id=?",
            (f"{name}-version", f"{name}-object"),
        )
    return f"{name}-content"


def _add_provider_secret_record(snapshot: Path) -> None:
    connection = sqlite3.connect(snapshot)
    try:
        instance_id = connection.execute(
            "SELECT instance_id FROM provider_secret_instances WHERE singleton_id='default'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO model_provider_configs (
              id, provider_kind, display_name, enabled, policy_json, secret_ref,
              model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
            ) VALUES (
              'provider-backup', 'openai-compatible', 'Backup Provider', 1, '{}',
              'local:secret-backup', '[\"model-a\"]', 'https://models.example.test/v1',
              'https://models.example.test', 'rev-1'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO provider_secret_records (
              id, provider_id, secret_version, algorithm, nonce_b64,
              ciphertext_b64, aad_json, secret_fingerprint, status
            ) VALUES (
              'secret-backup', 'provider-backup', 1, 'AES-256-GCM',
              :nonce, :ciphertext, :aad,
              'fingerprint', 'active'
            )
            """,
            {
                "nonce": base64.b64encode(b"0123456789ab").decode(),
                "ciphertext": base64.b64encode(b"0123456789abcdef").decode(),
                "aad": json.dumps(
                    {
                        "algorithm": "AES-256-GCM",
                        "instance_id": instance_id,
                        "provider_id": "provider-backup",
                        "secret_id": "secret-backup",
                        "version": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
        connection.commit()
    finally:
        connection.close()


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
    assert manifest["format_version"] == 4
    assert manifest["unmaterialized_text_versions"] == []
    assert manifest["provider_secret_recovery"] == {
        "encrypted_records_in_database": False,
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


def test_provider_secret_records_are_verified_and_preserved_in_backup(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_provider_secret_record(snapshot)
    bundle = tmp_path / "bundle-with-provider-secret"

    stage_backup(snapshot, root, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["format_version"] == 4
    assert manifest["provider_secret_recovery"]["encrypted_records_in_database"] is True
    assert len(verify_backup_bundle(bundle)) == 3

    restored = tmp_path / "restored.sqlite"
    shutil.copy2(bundle / "database.sqlite", restored)
    connection = sqlite3.connect(restored)
    try:
        row = connection.execute(
            "SELECT secret_version, algorithm, nonce_b64, ciphertext_b64, aad_json "
            "FROM provider_secret_records"
        ).fetchone()
        restored_instance_id = connection.execute(
            "SELECT instance_id FROM provider_secret_instances WHERE singleton_id='default'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert row == (
        1,
        "AES-256-GCM",
        base64.b64encode(b"0123456789ab").decode(),
        base64.b64encode(b"0123456789abcdef").decode(),
        json.dumps(
            {
                "algorithm": "AES-256-GCM",
                "instance_id": restored_instance_id,
                "provider_id": "provider-backup",
                "secret_id": "secret-backup",
                "version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )

    connection = sqlite3.connect(bundle / "database.sqlite")
    try:
        connection.execute("UPDATE provider_secret_records SET ciphertext_b64 = ''")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="checksum"):
        verify_backup_bundle(bundle)


def test_provider_recovery_metadata_must_match_database(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_provider_secret_record(snapshot)
    bundle = tmp_path / "bundle-with-mismatched-provider-metadata"
    stage_backup(snapshot, root, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider_secret_recovery"]["encrypted_records_in_database"] = False
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not match database"):
        verify_backup_bundle(bundle)


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


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE model_provider_configs SET secret_ref='local:absent'",
        "DELETE FROM provider_secret_records",
        "UPDATE provider_secret_records SET status='rotated'",
        "DELETE FROM model_provider_configs",
    ],
)
def test_backup_rejects_inconsistent_secret_references(tmp_path: Path, mutation: str) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_provider_secret_record(snapshot)
    with sqlite3.connect(snapshot) as connection:
        connection.execute(mutation)
    bundle = tmp_path / "invalid-secret-reference"
    stage_backup(snapshot, root, bundle)
    with pytest.raises(ValueError, match="provider secret reference"):
        verify_backup_bundle(bundle)


@pytest.mark.parametrize("status", ["rotated", "revoked"])
def test_backup_retains_inactive_secret_history(tmp_path: Path, status: str) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_provider_secret_record(snapshot)
    with sqlite3.connect(snapshot) as connection:
        connection.execute("UPDATE model_provider_configs SET secret_ref=NULL, enabled=0")
        connection.execute("UPDATE provider_secret_records SET status=?", (status,))
    bundle = tmp_path / "inactive-secret-history"
    stage_backup(snapshot, root, bundle)
    assert len(verify_backup_bundle(bundle)) == 3


def test_backup_rejects_plaintext_provider_reference(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_provider_secret_record(snapshot)
    with sqlite3.connect(snapshot) as connection:
        connection.execute(
            "UPDATE model_provider_configs SET secret_ref='sk-plaintext-provider-key'"
        )
    bundle = tmp_path / "plaintext-provider-reference"
    stage_backup(snapshot, root, bundle)
    with pytest.raises(ValueError, match="reference is invalid"):
        verify_backup_bundle(bundle)


def test_backup_records_legacy_pdf_text_that_was_never_materialized(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    legacy = _add_pdf(snapshot, root, "legacy", manifest=False)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["format_version"] == 4
    assert manifest["unmaterialized_text_versions"] == [legacy]
    assert (bundle / "objects" / "evidence" / "legacy.pdf").read_bytes() == b"%PDF-1.7 legacy"
    assert len(verify_backup_bundle(bundle)) == 4


@pytest.mark.parametrize("listed", [[], ["legacy-content", "invented-content"]])
def test_verification_rejects_unmaterialized_list_that_disagrees_with_database(
    tmp_path: Path, listed: list[str]
) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unmaterialized_text_versions"] = listed
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unmaterialized"):
        verify_backup_bundle(bundle)


def test_backup_verifies_pdf_manifest_against_its_parse_attempt(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "current", manifest=True)
    manifest_bytes = (root / "pdf-attempts" / "current" / "manifest.json").read_bytes()
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    assert (bundle / "objects" / "pdf-attempts" / "current" / "manifest.json").read_bytes() == (
        manifest_bytes
    )
    assert json.loads((bundle / "manifest.json").read_text())["unmaterialized_text_versions"] == []
    assert len(verify_backup_bundle(bundle)) == 5


def test_backup_still_aborts_on_corrupt_or_missing_pdf_manifest(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "current", manifest=True)
    manifest_path = root / "pdf-attempts" / "current" / "manifest.json"
    manifest_path.write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        stage_backup(snapshot, root, tmp_path / "corrupt")
    manifest_path.unlink()
    with pytest.raises(FileNotFoundError):
        stage_backup(snapshot, root, tmp_path / "missing")


def test_backup_still_aborts_on_empty_text_reference_outside_pdfs(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    with sqlite3.connect(snapshot) as connection:
        connection.execute("UPDATE content_versions SET text_artifact_uri=''")
    with pytest.raises(ValueError, match="materialized"):
        stage_backup(snapshot, root, tmp_path / "bundle")


def test_restore_prepares_bundle_with_legacy_and_current_pdfs(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    legacy = _add_pdf(snapshot, root, "legacy", manifest=False)
    _add_pdf(snapshot, root, "current", manifest=True)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    journal = ExternalEraseJournal(tmp_path / "independent-journal.jsonl", "synthetic-secret")
    journal.path.touch()
    survivors = prepare_restored_bundle(bundle, journal, project_root=Path.cwd())
    assert {item.relative_path for item in survivors} >= {
        "evidence/legacy.pdf",
        "evidence/current.pdf",
        "pdf-attempts/current/manifest.json",
    }
    with sqlite3.connect(bundle / "database.sqlite") as connection:
        assert connection.execute(
            "SELECT text_artifact_uri FROM content_versions WHERE id=?", (legacy,)
        ).fetchone() == ("",)


def _add_unlinked_attempt(
    snapshot: Path, root: Path, name: str, *, render: bool = False, image: bool = False
) -> dict[str, Path]:
    """Record a later parse attempt for a PDF whose text reference does not link to it."""
    attempt_root = root / "pdf-attempts" / f"{name}-later"
    attempt_root.mkdir(parents=True)
    files = {"manifest": attempt_root / "manifest.json"}
    files["manifest"].write_text(json.dumps({"blocks": [f"{name} later attempt"]}))
    if render:
        files["render"] = attempt_root / "page-1.png"
        files["render"].write_bytes(f"render {name}".encode())
    if image:
        files["image"] = attempt_root / "image-1.png"
        files["image"].write_bytes(f"image {name}".encode())

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    with sqlite3.connect(snapshot) as connection:
        connection.execute(
            "INSERT INTO pdf_tasks (id, evidence_object_id, options_hash, idempotency_key, "
            "state) VALUES (?, ?, 'options-later', ?, 'parsed')",
            (f"{name}-later-task", f"{name}-evidence", f"{name}-later-task"),
        )
        connection.execute(
            "INSERT INTO pdf_parse_attempts (id, task_id, evidence_object_id, backend, "
            "attempt_no, status, manifest_uri, manifest_sha256) "
            "VALUES (?, ?, ?, 'mineru', 1, 'succeeded', ?, ?)",
            (
                f"{name}-later-attempt",
                f"{name}-later-task",
                f"{name}-evidence",
                files["manifest"].as_uri(),
                digest(files["manifest"]),
            ),
        )
        connection.execute(
            "INSERT INTO pdf_pages (id, attempt_id, page_no, page_width, page_height, "
            "crop_box_json, render_uri, render_sha256) VALUES (?, ?, 1, 612, 792, '[]', ?, ?)",
            (
                f"{name}-later-page",
                f"{name}-later-attempt",
                files["render"].as_uri() if render else None,
                digest(files["render"]) if render else None,
            ),
        )
        if image:
            connection.execute(
                "INSERT INTO pdf_images (id, attempt_id, page_id, artifact_uri, sha256, "
                "media_type, bbox_json) VALUES (?, ?, ?, ?, ?, 'image/png', '[]')",
                (
                    f"{name}-later-image",
                    f"{name}-later-attempt",
                    f"{name}-later-page",
                    files["image"].as_uri(),
                    digest(files["image"]),
                ),
            )
    return files


def test_backup_includes_parse_attempt_manifest_no_text_reference_links_to(
    tmp_path: Path,
) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    files = _add_unlinked_attempt(snapshot, root, "legacy")
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    assert (
        bundle / "objects" / "pdf-attempts" / "legacy-later" / "manifest.json"
    ).read_bytes() == (files["manifest"].read_bytes())


def test_backup_includes_pdf_page_renders_and_images_by_their_digests(tmp_path: Path) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    files = _add_unlinked_attempt(snapshot, root, "legacy", render=True, image=True)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    staged = bundle / "objects" / "pdf-attempts" / "legacy-later"
    assert (staged / "page-1.png").read_bytes() == b"render legacy"
    assert (staged / "image-1.png").read_bytes() == b"image legacy"

    files["image"].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        stage_backup(snapshot, root, tmp_path / "tampered-image")
    files["image"].write_bytes(b"image legacy")
    files["render"].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        stage_backup(snapshot, root, tmp_path / "tampered-render")


def test_backup_skips_pdf_artifacts_that_are_not_local_files(tmp_path: Path) -> None:
    # MinerU images are recorded as artifact:// references, which erase never unlinks.
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    files = _add_unlinked_attempt(snapshot, root, "legacy", image=True)
    with sqlite3.connect(snapshot) as connection:
        connection.execute("UPDATE pdf_images SET artifact_uri='artifact://mineru/image-1.png'")
    files["image"].unlink()
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    assert not (bundle / "objects" / "pdf-attempts" / "legacy-later" / "image-1.png").exists()


def test_restore_relocates_pdf_attempt_artifacts_no_text_reference_links_to(
    tmp_path: Path,
) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    _add_unlinked_attempt(snapshot, root, "legacy", render=True, image=True)
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    journal = ExternalEraseJournal(tmp_path / "independent-journal.jsonl", "synthetic-secret")
    journal.path.touch()
    survivors = prepare_restored_bundle(bundle, journal, project_root=Path.cwd())
    staged = (bundle / "objects" / "pdf-attempts" / "legacy-later").resolve()
    assert {item.relative_path for item in survivors} >= {
        "pdf-attempts/legacy-later/manifest.json",
        "pdf-attempts/legacy-later/page-1.png",
        "pdf-attempts/legacy-later/image-1.png",
    }
    with sqlite3.connect(bundle / "database.sqlite") as connection:
        assert connection.execute(
            "SELECT a.manifest_uri, p.render_uri, i.artifact_uri FROM pdf_parse_attempts a "
            "JOIN pdf_pages p ON p.attempt_id = a.id JOIN pdf_images i ON i.attempt_id = a.id "
            "WHERE a.id = 'legacy-later-attempt'"
        ).fetchone() == (
            (staged / "manifest.json").as_uri(),
            (staged / "page-1.png").as_uri(),
            (staged / "image-1.png").as_uri(),
        )


@pytest.mark.parametrize("column", ["manifest_uri", "render_uri", "artifact_uri"])
def test_pdf_attempt_artifacts_outside_the_object_root_are_refused(
    tmp_path: Path, column: str
) -> None:
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    _add_unlinked_attempt(snapshot, root, "legacy", render=True, image=True)
    outside = tmp_path / "elsewhere" / "artifact.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"outside")
    table = {"manifest_uri": "pdf_parse_attempts", "render_uri": "pdf_pages"}.get(
        column, "pdf_images"
    )
    with sqlite3.connect(snapshot) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (outside.as_uri(),))
    with pytest.raises(ValueError, match="outside configured object root"):
        stage_backup(snapshot, root, tmp_path / "bundle")
    with pytest.raises(ValueError, match="escapes original object root"):
        relocate_object_references(snapshot, root, tmp_path / "restored-objects")


def test_restore_still_prepares_format_3_bundle_missing_unlinked_attempt_manifest(
    tmp_path: Path,
) -> None:
    # Format 3 bundles never carried attempt artifacts that no text reference linked to.
    snapshot, root, _ = _snapshot(tmp_path)
    _add_pdf(snapshot, root, "legacy", manifest=False)
    _add_unlinked_attempt(snapshot, root, "legacy")
    bundle = tmp_path / "bundle"
    stage_backup(snapshot, root, bundle)
    unlinked = "pdf-attempts/legacy-later/manifest.json"
    (bundle / "objects" / unlinked).unlink()
    (bundle / "objects" / unlinked).parent.rmdir()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["format_version"] = 3
    manifest["artifacts"] = [
        item for item in manifest["artifacts"] if item["relative_path"] != unlinked
    ]
    manifest_path.write_text(json.dumps(manifest))
    journal = ExternalEraseJournal(tmp_path / "independent-journal.jsonl", "synthetic-secret")
    journal.path.touch()
    survivors = prepare_restored_bundle(bundle, journal, project_root=Path.cwd())
    assert unlinked not in {item.relative_path for item in survivors}
    with sqlite3.connect(bundle / "database.sqlite") as connection:
        assert connection.execute(
            "SELECT manifest_uri FROM pdf_parse_attempts WHERE id='legacy-later-attempt'"
        ).fetchone() == ((bundle / "objects" / unlinked).resolve().as_uri(),)
