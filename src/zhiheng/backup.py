"""Content manifest for a consistent SQLite snapshot and its immutable artifacts.

This is a local staging primitive, not a complete encrypted backup workflow.
"""

from __future__ import annotations

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
            "format_version": 1,
            "original_object_root": str(object_root.resolve()),
            "database_sha256": _file_digest(snapshot),
            "artifacts": [asdict(item) for item in manifest],
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
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "original_object_root",
        "database_sha256",
        "artifacts",
    }:
        raise ValueError("unsupported backup manifest schema")
    if type(payload["format_version"]) is not int or payload["format_version"] != 1:
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
