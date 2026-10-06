"""Restore one manifest-bound Zhiheng restic snapshot into the serving location.

The external erase journal is always the newest local authority. The journal is
loaded from the configured path and is never restored from the old snapshot.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zhiheng.backup import BackupArtifact
from zhiheng.db.maintenance import acquire_database_lock
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import EraseJournalRecord, ExternalEraseJournal
from zhiheng.restore import prepare_restored_bundle, relocate_object_references

SNAPSHOT_PATTERN = re.compile(r"[0-9a-f]{64}")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class ResticRestoreError(RuntimeError):
    """Safe diagnostic: never carry command output or repository credentials."""

    def __init__(self, code: str, reason: str, elapsed: float) -> None:
        super().__init__(
            json.dumps(
                {
                    "error_code": code,
                    "reason": reason,
                    "elapsed_seconds": round(elapsed, 3),
                }
            )
        )


def _interrupt_restore(signum: int, frame: FrameType | None) -> None:
    # Let finally blocks clean staging and reap the child before exiting.
    # Repeated shutdown signals must not interrupt that cleanup.
    for termination in (signal.SIGTERM, signal.SIGINT):
        signal.signal(termination, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main() -> None:
    previous_handlers = {
        termination: signal.signal(termination, _interrupt_restore)
        for termination in (signal.SIGTERM, signal.SIGINT)
    }
    previous_umask = os.umask(0o077)
    try:
        try:
            _restore_from_env()
        except ResticRestoreError as exc:
            raise SystemExit(str(exc)) from None
    finally:
        os.umask(previous_umask)
        for termination, handler in previous_handlers.items():
            signal.signal(termination, handler)


def _restore_from_env() -> None:
    database = _required_path("ZHIHENG_DATABASE_PATH")
    object_root = _required_path("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH")
    journal_path = _required_path("ZHIHENG_ERASE_JOURNAL_PATH")
    _validate_restore_targets(database, object_root, journal_path)
    secret = _required_env("ZHIHENG_SECRET_KEY")
    snapshot_id = _required_env("ZHIHENG_RESTIC_SNAPSHOT_ID")
    if SNAPSHOT_PATTERN.fullmatch(snapshot_id) is None:
        raise SystemExit("ZHIHENG_RESTIC_SNAPSHOT_ID must be exactly 64 lowercase hex chars")
    _required_env("RESTIC_REPOSITORY")
    if not (os.environ.get("RESTIC_PASSWORD") or os.environ.get("RESTIC_PASSWORD_FILE")):
        raise SystemExit("configure RESTIC_PASSWORD or RESTIC_PASSWORD_FILE")

    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    object_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = os.environ.get("ZHIHENG_RESTIC_BINARY", "restic")
    project_root = Path(__file__).resolve().parents[1]

    db_lock = acquire_database_lock(str(database), exclusive=True)
    try:
        # Recover durable intent while the database is excluded, before taking
        # the shared journal lock used by the rest of the installation.
        ExternalEraseJournal(journal_path, secret).recover_pending()
        with _shared_journal_lock(journal_path):
            _refuse_sqlite_sidecars(database)
            journal = ExternalEraseJournal(journal_path, secret)
            records = journal.load()
            with tempfile.TemporaryDirectory(
                prefix=f"{database.name}.restic-restore.", dir=database.parent
            ) as temporary:
                temp_root = Path(temporary)
                restore_root = temp_root / "restored"
                _run_restic_restore(binary, snapshot_id, restore_root)
                bundle = _single_restored_bundle(restore_root)
                _refuse_sqlite_sidecars(bundle / "database.sqlite")
                survivors = prepare_restored_bundle(bundle, journal, project_root=project_root)
                _install_surviving_objects(bundle / "objects", object_root, survivors)
                restored_database = bundle / "database.sqlite"
                relocate_object_references(
                    restored_database, (bundle / "objects").resolve(), object_root.resolve()
                )
                _replay_latest_journal_again(restored_database, object_root, journal, records)
                _compact_and_verify_database(restored_database)
                _refuse_sqlite_sidecars(database)
                _replace_database(restored_database, database)
            _fsync_directory(database.parent)
        print(
            json.dumps(
                {
                    "restored_snapshot_id": snapshot_id,
                    "format_version": 1,
                    "provider_secret_recovery": "native-keyring-required",
                }
            )
        )
    finally:
        os.close(db_lock)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} must be explicitly configured")
    return value


def _required_path(name: str) -> Path:
    return Path(_required_env(name)).expanduser().resolve()


def _validate_restore_targets(database: Path, object_root: Path, journal_path: Path) -> None:
    if database == Path(database.anchor) or object_root == Path(object_root.anchor):
        raise SystemExit("restore targets must not be filesystem roots")
    if database.exists() and database.is_dir():
        raise SystemExit("ZHIHENG_DATABASE_PATH must be a database file path, not a directory")
    if object_root in (database, journal_path):
        raise SystemExit("object root must be separate from database and erase journal paths")
    if database.is_relative_to(object_root) or journal_path.is_relative_to(object_root):
        raise SystemExit("object root must not contain the database or erase journal")


@contextmanager
def _shared_journal_lock(journal_path: Path) -> Iterator[None]:
    descriptor = os.open(journal_path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _refuse_sqlite_sidecars(database: Path) -> None:
    existing = [Path(f"{database}{suffix}") for suffix in SIDECAR_SUFFIXES]
    if any(path.exists() for path in existing):
        raise RuntimeError("refusing restore while SQLite sidecar files exist")


def _run_restic_restore(binary: str, snapshot_id: str, target: Path) -> None:
    target.mkdir(mode=0o700)
    raw_timeout = os.environ.get("ZHIHENG_RESTIC_RESTORE_TIMEOUT_SECONDS", "30")
    try:
        timeout = float(raw_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        raise ResticRestoreError(
            "RESTIC_RESTORE_CONFIG_INVALID", "Restore timeout must be positive and finite.", 0
        ) from None
    started = time.monotonic()
    try:
        # A separate process group also bounds transport subprocesses (for example SSH).
        # Discard raw output: restic may echo repository URLs or credentials on failure.
        with subprocess.Popen(
            [binary, "restore", snapshot_id, "--target", str(target), "--verify"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ResticRestoreError(
                    "RESTIC_RESTORE_TIMEOUT",
                    "Restore exceeded its time limit; retry or increase the configured timeout.",
                    time.monotonic() - started,
                ) from None
            finally:
                # The outer runner can terminate this CLI before its own timeout.
                # Reap the independent restic group on every exit, including signals.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    except OSError:
        raise ResticRestoreError(
            "RESTIC_RESTORE_UNAVAILABLE",
            "Unable to execute restic; check its installation.",
            time.monotonic() - started,
        ) from None
    errors = {
        11: ("RESTIC_REPOSITORY_LOCKED", "Repository is locked; retry after the active operation."),
        12: (
            "RESTIC_AUTH_FAILED",
            "Repository authentication failed; check configured credentials.",
        ),
    }
    if returncode != 0:
        code, reason = errors.get(
            returncode,
            ("RESTIC_RESTORE_FAILED", "Restic restore failed; verify repository and snapshot."),
        )
        raise ResticRestoreError(code, reason, time.monotonic() - started)


def _single_restored_bundle(root: Path) -> Path:
    manifests = list(root.rglob("manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError("restic snapshot must contain exactly one Zhiheng backup manifest")
    return manifests[0].parent


def _install_surviving_objects(
    staged_root: Path, object_root: Path, survivors: tuple[BackupArtifact, ...]
) -> None:
    object_root = _ensure_object_root(object_root)
    directories_to_fsync: set[Path] = {object_root}
    for artifact in survivors:
        source = staged_root / artifact.relative_path
        relative = Path(artifact.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("refusing to install object outside object root")
        destination = object_root / artifact.relative_path
        destination_parent = _ensure_safe_object_directory(
            object_root, relative.parent, directories_to_fsync
        )
        destination = destination_parent / relative.name
        if destination.exists():
            if (
                _file_digest(destination) != artifact.sha256
                or destination.stat().st_size != artifact.byte_size
            ):
                raise RuntimeError(f"refusing to overwrite conflicting object: {destination}")
            continue
        temporary: Path | None = None
        try:
            fd, raw_temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(raw_temporary)
            with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if _file_digest(temporary) != artifact.sha256 or temporary.stat().st_size != (
                artifact.byte_size
            ):
                raise RuntimeError("installed object checksum or size mismatch")
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                if (
                    _file_digest(destination) != artifact.sha256
                    or destination.stat().st_size != artifact.byte_size
                ):
                    raise RuntimeError(
                        f"refusing to overwrite conflicting object: {destination}"
                    ) from exc
            directories_to_fsync.add(destination.parent)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    for directory in sorted(directories_to_fsync, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _ensure_object_root(object_root: Path) -> Path:
    if object_root.exists() and object_root.is_symlink():
        raise RuntimeError("refusing symlink object root")
    object_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not object_root.is_dir():
        raise RuntimeError("object root must be a directory")
    return object_root.resolve()


def _ensure_safe_object_directory(
    object_root: Path, relative_parent: Path, directories_to_fsync: set[Path]
) -> Path:
    current = object_root
    for part in relative_parent.parts:
        if part in {"", "."}:
            continue
        candidate = current / part
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise RuntimeError("refusing symlink inside object root")
            if not candidate.is_dir():
                raise RuntimeError("object path parent must be a directory")
        else:
            candidate.mkdir(mode=0o700)
            directories_to_fsync.add(current)
        current = candidate
    return current


def _replay_latest_journal_again(
    database: Path,
    object_root: Path,
    journal: ExternalEraseJournal,
    records: list[EraseJournalRecord],
) -> None:
    knowledge_request_ids = [
        record.request_id for record in records if record.target_type == "knowledge_object"
    ]
    if knowledge_request_ids:
        placeholders = ",".join("?" for _ in knowledge_request_ids)
        connection = sqlite3.connect(database)
        try:
            with connection:
                connection.execute(
                    f"""
                    UPDATE privacy_physical_erases
                    SET status = 'pending', completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE erase_request_id IN ({placeholders})
                    """,
                    knowledge_request_ids,
                )
        finally:
            connection.close()

    engine = create_engine(f"sqlite:///{database.resolve()}")
    try:
        with Session(engine) as session:
            PrivacyEraseService(journal, object_store_root=object_root).replay_external_journal(
                session
            )
            session.commit()
    finally:
        engine.dispose()


def _compact_and_verify_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # The staged database is already compacted; changing journal mode after
        # replay can race a lingering SQLite checkpoint lock.
        connection.execute("VACUUM")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("restored database integrity check failed")
    finally:
        connection.close()
    _clear_sqlite_sidecars(database)


def _clear_sqlite_sidecars(database: Path) -> None:
    """Remove only transient sidecars created while compacting staged SQLite."""
    for path in (Path(f"{database}{suffix}") for suffix in SIDECAR_SUFFIXES):
        if path.is_symlink():
            raise RuntimeError("refusing symlink SQLite sidecar")
        if path.exists():
            path.unlink()


def _replace_database(source: Path, destination: Path) -> None:
    if destination.name.endswith(SIDECAR_SUFFIXES):
        raise RuntimeError("refusing to replace a SQLite sidecar path")
    fd = os.open(source, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(source, 0o600)
    os.replace(source, destination)


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
