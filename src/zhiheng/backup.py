"""Content manifest for a consistent SQLite snapshot and its immutable artifacts.

This is a local staging primitive, not a complete encrypted backup workflow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from zhiheng.db.maintenance import acquire_database_lock


@dataclass(frozen=True)
class BackupArtifact:
    relative_path: str
    sha256: str
    byte_size: int


def stage_backup(database: Path, object_root: Path, destination: Path) -> None:
    """Stage a SQLite online snapshot plus all referenced bytes for encryption.

    The destination must be new. A failed stage is never a publishable backup.
    A concurrent erase can make copying fail; retry with a fresh snapshot.
    """
    if not database.is_file():
        raise FileNotFoundError(database)
    destination.mkdir(mode=0o700)
    descriptor = acquire_database_lock(str(database), exclusive=False)
    try:
        snapshot = destination / "database.sqlite"
        fd = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        source = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("VACUUM")
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("backup snapshot integrity check failed")
        finally:
            target.close()
            source.close()
        manifest = collect_artifact_manifest(snapshot, object_root)
        for artifact in manifest:
            path = destination / "objects" / artifact.relative_path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (object_root / artifact.relative_path).open("rb") as source_file:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target_file:
                    shutil.copyfileobj(source_file, target_file)
                    target_file.flush()
                    os.fsync(target_file.fileno())
            if _file_digest(path) != artifact.sha256 or path.stat().st_size != artifact.byte_size:
                raise ValueError("artifact changed while staging backup")
        payload = {
            "format_version": 2,
            "original_object_root": str(object_root.resolve()),
            "database_sha256": _file_digest(snapshot),
            "artifacts": [asdict(item) for item in manifest],
            "provider_secret_recovery": {
                "encrypted_records_in_database": _has_provider_secret_records(snapshot),
                "master_key_external_dependency": "native-keyring",
                "missing_master_key_behavior": "provider_reentry_required",
            },
        }
        fd = os.open(destination / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_backup_bundle(bundle: Path) -> tuple[BackupArtifact, ...]:
    """Verify all restored bytes and reject unlisted files before any installation."""
    root = bundle.resolve()
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("backup bundle must not contain symlinks")
    payload = json.loads((root / "manifest.json").read_text())
    if not isinstance(payload, dict) or set(payload) not in ({
        "format_version",
        "original_object_root",
        "database_sha256",
        "artifacts",
    }, {
        "format_version",
        "original_object_root",
        "database_sha256",
        "artifacts",
        "provider_secret_recovery",
    }):
        raise ValueError("unsupported backup manifest schema")
    if type(payload["format_version"]) is not int or payload["format_version"] not in {1, 2}:
        raise ValueError("unsupported backup format version")
    if (
        not isinstance(payload["original_object_root"], str)
        or not Path(payload["original_object_root"]).is_absolute()
    ):
        raise ValueError("backup original object root must be absolute")
    if _file_digest(root / "database.sqlite") != payload["database_sha256"]:
        raise ValueError("backup database checksum mismatch")
    if not isinstance(payload["artifacts"], list):
        raise ValueError("backup artifacts must be a list")
    recovery = payload.get("provider_secret_recovery")
    if payload["format_version"] == 2 and not isinstance(recovery, dict):
        raise ValueError("provider secret recovery metadata is required")
    if recovery is not None and (
        not isinstance(recovery, dict)
        or set(recovery) != {
            "encrypted_records_in_database",
            "master_key_external_dependency",
            "missing_master_key_behavior",
        }
        or not isinstance(recovery["encrypted_records_in_database"], bool)
        or recovery["master_key_external_dependency"] != "native-keyring"
        or recovery["missing_master_key_behavior"] != "provider_reentry_required"
    ):
        raise ValueError("invalid provider secret recovery metadata")
    if recovery is not None and recovery["encrypted_records_in_database"]:
        _verify_provider_secret_records(root / "database.sqlite")
    database_has_secrets = _has_provider_secret_records(root / "database.sqlite")
    if recovery is not None and recovery["encrypted_records_in_database"] != database_has_secrets:
        raise ValueError("provider secret recovery metadata does not match database")
    expected_files = {"manifest.json", "database.sqlite"}
    artifacts: list[BackupArtifact] = []
    for item in payload["artifacts"]:
        if not isinstance(item, dict) or set(item) != {"relative_path", "sha256", "byte_size"}:
            raise ValueError("invalid backup artifact schema")
        relative = item["relative_path"]
        if not isinstance(relative, str) or not relative:
            raise ValueError("invalid backup relative path")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or relative != path.as_posix():
            raise ValueError("invalid backup relative path")
        filename = f"objects/{relative}"
        if filename in expected_files:
            raise ValueError("duplicate backup artifact path")
        expected_files.add(filename)
        if not isinstance(item["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", item["sha256"]):
            raise ValueError("invalid backup artifact checksum")
        size = item["byte_size"]
        if type(size) is not int or size < 0:
            raise ValueError("invalid backup artifact size")
        artifact_path = root / filename
        if _file_digest(artifact_path) != item["sha256"] or artifact_path.stat().st_size != size:
            raise ValueError("backup artifact checksum or size mismatch")
        artifacts.append(BackupArtifact(relative, item["sha256"], size))
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise ValueError("backup bundle contains unlisted or missing files")
    return tuple(artifacts)


def _has_provider_secret_records(database: Path) -> bool:
    connection = sqlite3.connect(database)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_secret_records'"
        ).fetchone()
        if table is None:
            return False
        return bool(connection.execute("SELECT 1 FROM provider_secret_records LIMIT 1").fetchone())
    finally:
        connection.close()


def _verify_provider_secret_records(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('provider_secret_instances', 'provider_secret_records')"
            )
        }
        if tables != {"provider_secret_instances", "provider_secret_records"}:
            raise ValueError("provider secret tables are missing from restored database")
        instance = connection.execute(
            "SELECT instance_id FROM provider_secret_instances WHERE singleton_id='default'"
        ).fetchone()
        if instance is None or not isinstance(instance[0], str) or not instance[0]:
            raise ValueError("provider secret instance metadata is incomplete")
        instance_id = instance[0]
        rows = connection.execute(
            "SELECT id, provider_id, secret_version, algorithm, nonce_b64, ciphertext_b64, "
            "aad_json FROM provider_secret_records"
        ).fetchall()
        providers = {
            str(provider_id): str(secret_ref)
            for provider_id, secret_ref in connection.execute(
                "SELECT id, secret_ref FROM model_provider_configs"
            ).fetchall()
            if isinstance(provider_id, str) and isinstance(secret_ref, str)
        }
        active_ids: set[str] = set()
        record_ids = {str(row[0]) for row in rows}
        for secret_ref in providers.values():
            if secret_ref.startswith("local:") and secret_ref.removeprefix("local:") not in record_ids:
                raise ValueError("restored provider secret reference is dangling")
        for secret_id, provider_id, version, algorithm, nonce, ciphertext, aad in rows:
            if provider_id not in providers:
                raise ValueError("restored provider secret references an unknown provider")
            if providers[provider_id].startswith("local:") and providers[provider_id] != f"local:{secret_id}":
                raise ValueError("restored provider secret reference is inconsistent")
            if not isinstance(version, int) or version < 1 or algorithm != "AES-256-GCM":
                raise ValueError("restored provider secret metadata is invalid")
            try:
                nonce_bytes = base64.b64decode(nonce, validate=True)
                ciphertext_bytes = base64.b64decode(ciphertext, validate=True)
                aad_payload = json.loads(aad)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("restored provider secret ciphertext is invalid") from exc
            if len(nonce_bytes) != 12 or len(ciphertext_bytes) < 16:
                raise ValueError("restored provider secret ciphertext is incomplete")
            if aad_payload != {
                "algorithm": algorithm,
                "instance_id": instance_id,
                "provider_id": provider_id,
                "secret_id": secret_id,
                "version": version,
            }:
                raise ValueError("restored provider secret authenticated data is invalid")
            status = connection.execute(
                "SELECT status FROM provider_secret_records WHERE id = ?", (secret_id,)
            ).fetchone()
            if status is not None and status[0] == "active":
                active_ids.add(str(secret_id))
        for secret_id in active_ids:
            if not any(secret_ref == f"local:{secret_id}" for secret_ref in providers.values()):
                raise ValueError("restored active provider secret is orphaned")
    finally:
        connection.close()


def collect_artifact_manifest(snapshot: Path, object_root: Path) -> tuple[BackupArtifact, ...]:
    """Read references from a completed snapshot, then verify files outside SQLite.

    Caller must supply a private, completed SQLite backup, never the live DB.
    Soft-deleted and candidate objects remain recoverable; privacy-erased ones do not.
    Missing or corrupt referenced artifacts abort collection instead of producing
    a deceptively successful partial backup.
    """
    root = object_root.resolve()
    connection = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    try:
        references = connection.execute(
            """
            SELECT eo.object_uri, eo.sha256, eo.byte_size
            FROM knowledge_versions kv
            JOIN knowledge_objects ko ON ko.id = kv.knowledge_object_id
            JOIN content_versions cv ON cv.id = kv.content_version_id
            JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
            WHERE ko.lifecycle_status <> 'privacy_erased'
              AND eo.status <> 'privacy_erased' AND cv.status <> 'privacy_erased'
            UNION
            SELECT cv.text_artifact_uri, cv.content_sha256, NULL
            FROM knowledge_versions kv
            JOIN knowledge_objects ko ON ko.id = kv.knowledge_object_id
            JOIN content_versions cv ON cv.id = kv.content_version_id
            WHERE ko.lifecycle_status <> 'privacy_erased' AND cv.status <> 'privacy_erased'
            UNION
            SELECT kv.markdown_uri, NULL, NULL
            FROM knowledge_versions kv
            JOIN knowledge_objects ko ON ko.id = kv.knowledge_object_id
            WHERE ko.lifecycle_status <> 'privacy_erased' AND kv.markdown_uri IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()

    artifacts: dict[str, BackupArtifact] = {}
    for uri, expected_hash, expected_size in references:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("backup requires local, materialized artifact references")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("backup artifact is outside configured object root")
        digest = hashlib.sha256()
        byte_size = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                byte_size += len(block)
        actual_hash = digest.hexdigest()
        if expected_hash is not None and actual_hash != expected_hash:
            raise ValueError("backup artifact hash does not match authoritative metadata")
        if expected_size is not None and byte_size != expected_size:
            raise ValueError("backup artifact size does not match authoritative metadata")
        relative = path.relative_to(root).as_posix()
        artifacts[relative] = BackupArtifact(relative, actual_hash, byte_size)
    return tuple(artifacts[key] for key in sorted(artifacts))
