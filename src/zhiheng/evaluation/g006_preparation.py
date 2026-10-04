"""Isolated preparation helpers for protected G006 evaluation fixtures.

The caches in this module contain only empty infrastructure fixtures.  Every
caller receives a private copy before it writes evidence, so evaluation runs
and their authorization records remain independent.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config

_LOCK = threading.RLock()
_PROCESS_TEMPLATE_LOCKS_LOCK = threading.Lock()
_CACHE_DIRECTORY = tempfile.TemporaryDirectory(prefix="zhiheng-g006-preparation-")
_CACHE_ROOT = Path(_CACHE_DIRECTORY.name)


@dataclass(frozen=True, slots=True)
class _Template:
    path: Path
    digest: str


_DATABASE_TEMPLATES: dict[tuple[str, str], _Template] = {}
_RESTIC_TEMPLATES: dict[tuple[str, str, str, str], _Template] = {}
_PROCESS_TEMPLATE_LOCKS: dict[Path, _ProcessTemplateLock] = {}


class _ProcessTemplateLock:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.shared_descriptor: int | None = None


def prepare_migrated_database(project_root: Path, database: Path) -> bool:
    """Copy a migration-validated empty database into ``database``.

    The template key includes every migration and Alembic configuration byte,
    so changing the schema automatically creates a new template.  The return
    value reports whether an existing template was reused.
    """

    project_root = project_root.resolve()
    database = database.resolve()
    key = (str(project_root), _migration_fingerprint(project_root))
    template_path = _CACHE_ROOT / f"database-{key[1]}.sqlite"
    with _LOCK:
        template = _DATABASE_TEMPLATES.get(key)
    with _shared_destination_lock(template_path):
        if template is not None and _valid_file_template(template):
            reused = True
        elif _database_template_is_healthy(template_path):
            template = _Template(template_path, _file_digest(template_path))
            reused = True
            with _LOCK:
                _DATABASE_TEMPLATES[key] = template
        else:
            template = None
        if template is not None:
            _assert_file_template_healthy(template.path)
            database.parent.mkdir(parents=True, exist_ok=True)
            _install_database_copy(template.path, database, template.digest)
            return reused
    with _exclusive_destination_lock(template_path):
        with _LOCK:
            template = _DATABASE_TEMPLATES.get(key)
        reused = template is not None and _valid_file_template(template)
        if not reused:
            if _database_template_is_healthy(template_path):
                template = _Template(template_path, _file_digest(template_path))
                reused = True
            else:
                _build_database_template(project_root, template_path)
                template = _Template(template_path, _file_digest(template_path))
            with _LOCK:
                _DATABASE_TEMPLATES[key] = template
    if template is None:
        raise RuntimeError("database preparation template was not initialized")
    with _shared_destination_lock(template_path):
        _assert_file_template_healthy(template.path)
        database.parent.mkdir(parents=True, exist_ok=True)
        _install_database_copy(template.path, database, template.digest)
    return reused


def prepare_restic_repository(
    *,
    binary: str,
    repository: Path,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> bool:
    """Copy an empty initialized repository and return whether it was reused.

    ``run`` is injected so recovery tests retain the existing diagnostic and
    failure behavior.  Only the empty repository layout is cached; snapshots
    and restored data are always created in the caller's private repository.
    """

    password_fingerprint = _restic_password_fingerprint(environment)
    if environment.get("RESTIC_PASSWORD_COMMAND"):
        raise ValueError("RESTIC_PASSWORD_COMMAND is unsupported for cached G006 preparation")
    resolved_binary = str(Path(shutil.which(binary) or binary).resolve())
    version = _restic_version(
        binary=binary,
        project_root=project_root,
        environment=environment,
        run=run,
    )
    key = (str(project_root.resolve()), resolved_binary, version, password_fingerprint)
    template_path = _CACHE_ROOT / f"restic-{hashlib.sha256(repr(key).encode()).hexdigest()}"
    with _LOCK:
        template = _RESTIC_TEMPLATES.get(key)
    with _shared_destination_lock(template_path):
        if template is not None and _valid_directory_template(template):
            reused = True
        elif template is None and _restic_template_is_healthy(
            template_path=template_path,
            binary=binary,
            project_root=project_root,
            environment=environment,
            run=run,
        ):
            template = _Template(template_path, _directory_digest(template_path))
            reused = True
            with _LOCK:
                _RESTIC_TEMPLATES[key] = template
        else:
            template = None
        if template is not None:
            repository.parent.mkdir(parents=True, exist_ok=True)
            _install_restic_copy(template.path, repository, template.digest)
            return reused
    with _exclusive_destination_lock(template_path):
        with _LOCK:
            template = _RESTIC_TEMPLATES.get(key)
        reused = template is not None and _valid_directory_template(template)
        if not reused:
            if template is None and _restic_template_is_healthy(
                template_path=template_path,
                binary=binary,
                project_root=project_root,
                environment=environment,
                run=run,
            ):
                reused = True
            else:
                _build_restic_template(
                    template_path=template_path,
                    binary=binary,
                    project_root=project_root,
                    environment=environment,
                    run=run,
                )
            template = _Template(template_path, _directory_digest(template_path))
            with _LOCK:
                _RESTIC_TEMPLATES[key] = template
    if template is None:
        raise RuntimeError("restic preparation template was not initialized")
    with _shared_destination_lock(template_path):
        repository.parent.mkdir(parents=True, exist_ok=True)
        _install_restic_copy(template.path, repository, template.digest)
    return reused


def _migration_fingerprint(project_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((project_root / "migrations").rglob("*.py")):
        digest.update(str(path.relative_to(project_root)).encode())
        digest.update(path.read_bytes())
    digest.update((project_root / "alembic.ini").read_bytes())
    src_root = project_root / "src" / "zhiheng"
    for path in sorted(src_root.rglob("*.py")):
        digest.update(str(path.relative_to(project_root)).encode())
        digest.update(path.read_bytes())
    for distribution in ("alembic", "sqlalchemy"):
        digest.update(distribution.encode())
        digest.update(_package_version(distribution).encode())
    return digest.hexdigest()


def _migrate_database_template(project_root: Path, template: Path) -> None:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(project_root / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{template}")
    command.upgrade(config, "head")


def _replace_file_template(path: Path, build: Callable[[Path], None]) -> None:
    _remove_path(path)
    path.with_suffix(f"{path.suffix}-wal").unlink(missing_ok=True)
    path.with_suffix(f"{path.suffix}-shm").unlink(missing_ok=True)
    try:
        build(path)
        _checkpoint_sqlite(path)
        _assert_file_template_healthy(path)
        _make_private(path)
    except BaseException:
        _remove_path(path)
        path.with_suffix(f"{path.suffix}-wal").unlink(missing_ok=True)
        path.with_suffix(f"{path.suffix}-shm").unlink(missing_ok=True)
        raise


def _replace_directory_template(path: Path, build: Callable[[Path], None]) -> None:
    _remove_path(path)
    try:
        build(path)
        if not path.is_dir():
            raise RuntimeError(f"template builder did not create directory: {path}")
        _make_private(path)
    except BaseException:
        _remove_path(path)
        raise


def _build_database_template(project_root: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.g006-template-",
        suffix=".sqlite",
        dir=destination.parent,
    )
    os.close(descriptor)
    staging = Path(temporary_name)
    installed = False
    try:
        _replace_file_template(
            staging,
            lambda path: _migrate_database_template(project_root, path),
        )
        _install_staged_path(staging, destination)
        _write_digest_manifest(destination, _file_digest(destination))
        installed = True
    finally:
        if not installed:
            _remove_path(staging)


def _build_restic_template(
    *,
    template_path: Path,
    binary: str,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> None:
    staging = Path(
        tempfile.mkdtemp(prefix=f".{template_path.name}.g006-template-", dir=template_path.parent)
    )
    installed = False
    try:
        _replace_directory_template(
            staging,
            lambda path: _initialize_empty_restic_repository(
                binary=binary,
                repository=path,
                project_root=project_root,
                environment=environment,
                run=run,
            ),
        )
        _install_staged_path(staging, template_path)
        _write_digest_manifest(template_path, _directory_digest(template_path))
        installed = True
    finally:
        if not installed:
            _remove_path(staging)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _install_staged_path(staging: Path, destination: Path) -> None:
    """Atomically publish a staged file or directory while retaining rollback."""

    backup: Path | None = None
    if os.path.lexists(destination):
        backup = destination.with_name(f".{destination.name}.g006-old-{uuid.uuid4().hex}")
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except BaseException:
        if backup is not None and not os.path.lexists(destination):
            os.replace(backup, destination)
        raise
    if backup is not None:
        _remove_path(backup)


def _install_database_copy(template: Path, destination: Path, digest: str) -> None:
    """Install a verified database copy without exposing a partial target."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.g006-",
        suffix=".sqlite",
        dir=destination.parent,
    )
    os.close(descriptor)
    staging = Path(temporary_name)
    installed = False
    try:
        shutil.copy2(template, staging)
        _make_private(staging)
        if _file_digest(staging) != digest or not _file_template_is_healthy(staging):
            raise RuntimeError("prepared database copy failed integrity verification")
        try:
            os.link(staging, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite prepared database: {destination}"
            ) from exc
        installed = True
        _make_private(destination)
    except BaseException:
        if installed:
            destination.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)


def _install_restic_copy(template: Path, destination: Path, digest: str) -> None:
    """Install a verified restic directory with a cooperative cross-process lock."""

    with _exclusive_destination_lock(destination):
        if os.path.lexists(destination):
            raise FileExistsError(
                f"refusing to overwrite prepared restic repository: {destination}"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.g006-", dir=destination.parent)
        )
        installed = False
        try:
            shutil.copytree(template, staging, dirs_exist_ok=True)
            _make_private(staging)
            if _directory_digest(staging) != digest:
                raise RuntimeError("prepared restic repository copy failed integrity verification")
            staging.rename(destination)
            installed = True
        finally:
            if not installed:
                try:
                    shutil.rmtree(staging)
                except OSError as exc:
                    raise RuntimeError(
                        f"failed to clean restic preparation staging directory: {staging}"
                    ) from exc


def _process_template_lock(destination: Path) -> _ProcessTemplateLock:
    lock_path = destination.with_name(f".{destination.name}.g006-lock")
    with _PROCESS_TEMPLATE_LOCKS_LOCK:
        return _PROCESS_TEMPLATE_LOCKS.setdefault(lock_path, _ProcessTemplateLock())


@contextmanager
def _exclusive_destination_lock(destination: Path) -> Iterator[None]:
    state = _process_template_lock(destination)
    with state.condition:
        while state.writer or state.readers:
            state.condition.wait()
        state.writer = True
    lock_path = destination.with_name(f".{destination.name}.g006-lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        try:
            if descriptor is not None:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        finally:
            with state.condition:
                state.writer = False
                state.condition.notify_all()


@contextmanager
def _shared_destination_lock(destination: Path) -> Iterator[None]:
    state = _process_template_lock(destination)
    with state.condition:
        while state.writer:
            state.condition.wait()
        if state.readers == 0:
            lock_path = destination.with_name(f".{destination.name}.g006-lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
            except BaseException:
                os.close(descriptor)
                raise
            state.shared_descriptor = descriptor
        state.readers += 1
    try:
        yield
    finally:
        with state.condition:
            state.readers -= 1
            if state.readers == 0:
                shared_descriptor = state.shared_descriptor
                state.shared_descriptor = None
                if shared_descriptor is not None:
                    fcntl.flock(shared_descriptor, fcntl.LOCK_UN)
                    os.close(shared_descriptor)
                state.condition.notify_all()


def _initialize_empty_restic_repository(
    *,
    binary: str,
    repository: Path,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> None:
    template_environment = dict(environment)
    template_environment["RESTIC_REPOSITORY"] = str(repository)
    run([binary, "init"], project_root, template_environment)
    _assert_empty_restic_repository(
        binary=binary,
        repository=repository,
        project_root=project_root,
        environment=template_environment,
        run=run,
    )


def _valid_file_template(template: _Template) -> bool:
    return (
        template.path.is_file()
        and _file_digest(template.path) == template.digest
        and _file_template_is_healthy(template.path)
    )


def _valid_directory_template(template: _Template) -> bool:
    return template.path.is_dir() and _directory_digest(template.path) == template.digest


def _restic_template_is_healthy(
    *,
    template_path: Path,
    binary: str,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> bool:
    if not template_path.is_dir():
        return False
    manifest = _read_digest_manifest(template_path)
    if manifest is None or _directory_digest(template_path) != manifest:
        return False
    try:
        _assert_empty_restic_repository(
            binary=binary,
            repository=template_path,
            project_root=project_root,
            environment=environment,
            run=run,
        )
    except Exception:
        return False
    return True


def _database_template_is_healthy(path: Path) -> bool:
    if not path.is_file():
        return False
    manifest = _read_digest_manifest(path)
    return (
        manifest is not None
        and _file_digest(path) == manifest
        and _file_template_is_healthy(path)
    )


def _digest_manifest_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.g006-digest")


def _write_digest_manifest(path: Path, digest: str) -> None:
    manifest = _digest_manifest_path(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{manifest.name}-", dir=manifest.parent)
    os.close(descriptor)
    staging = Path(temporary_name)
    try:
        staging.write_text(digest, encoding="ascii")
        os.replace(staging, manifest)
    finally:
        staging.unlink(missing_ok=True)


def _read_digest_manifest(path: Path) -> str | None:
    try:
        return _digest_manifest_path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _assert_file_template_healthy(path: Path) -> None:
    if not _file_template_is_healthy(path):
        raise RuntimeError(f"database template failed integrity check: {path}")


def _file_template_is_healthy(path: Path) -> bool:
    import sqlite3

    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            return row is not None and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def _checkpoint_sqlite(path: Path) -> None:
    import sqlite3

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _assert_empty_restic_repository(
    *,
    binary: str,
    repository: Path,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> None:
    template_environment = dict(environment)
    template_environment["RESTIC_REPOSITORY"] = str(repository)
    output = run([binary, "snapshots", "--json"], project_root, template_environment).strip()
    if output not in ("", "[]"):
        raise RuntimeError("restic preparation template must not contain snapshots")


def _restic_version(
    *,
    binary: str,
    project_root: Path,
    environment: dict[str, str],
    run: Callable[[list[str], Path, dict[str, str]], str],
) -> str:
    return run([binary, "version"], project_root, dict(environment)).strip()


def _restic_password_fingerprint(environment: dict[str, str]) -> str:
    digest = hashlib.sha256()
    if password := environment.get("RESTIC_PASSWORD"):
        digest.update(b"RESTIC_PASSWORD")
        digest.update(password.encode())
        return digest.hexdigest()
    if password_file := environment.get("RESTIC_PASSWORD_FILE"):
        digest.update(b"RESTIC_PASSWORD_FILE")
        path = Path(password_file)
        digest.update(str(path.resolve()).encode())
        if path.is_file():
            digest.update(path.read_bytes())
        return digest.hexdigest()
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        digest.update(str(relative).encode())
        if item.is_file():
            digest.update(b"file")
            digest.update(item.read_bytes())
        elif item.is_dir():
            digest.update(b"dir")
    return digest.hexdigest()


def _make_private(path: Path) -> None:
    if path.is_file():
        os.chmod(path, 0o600)
        return
    for item in path.rglob("*"):
        if item.is_dir():
            os.chmod(item, 0o700)
        elif item.is_file():
            os.chmod(item, 0o600)
    os.chmod(path, 0o700)


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "uninstalled"
