from __future__ import annotations

import fcntl
import multiprocessing
import os
import shutil
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command as alembic_command
from alembic.config import Config

from zhiheng.evaluation import g006_preparation as preparation


def _multiprocess_fake_restic_run(args: list[str], root: Path, env: dict[str, str]) -> str:
    del root
    command = args[1]
    if command == "version":
        return "restic 0.17.0 compiled with multiprocess-test"
    repository = Path(env["RESTIC_REPOSITORY"])
    if command == "init":
        repository.mkdir(parents=True)
        (repository / "config").write_text("config", encoding="utf-8")
        (repository / "keys").mkdir()
        (repository / "keys" / "key").write_text("key", encoding="utf-8")
        return ""
    if command == "snapshots":
        return "[]"
    raise AssertionError(args)


def _prepare_same_restic_target_worker(
    start: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue[tuple[str, str]],
    root: Path,
    target: Path,
) -> None:
    start.wait()
    try:
        preparation.prepare_restic_repository(
            binary="restic",
            repository=target,
            project_root=root,
            environment={"RESTIC_PASSWORD": "protected-multiprocess-password"},
            run=_multiprocess_fake_restic_run,
        )
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__))
    else:
        result_queue.put(("ok", "created"))


def _prepare_same_database_target_worker(
    start: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue[tuple[str, str]],
    root: Path,
    target: Path,
) -> None:
    start.wait()
    try:
        preparation.prepare_migrated_database(root, target)
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__))
    else:
        result_queue.put(("ok", "created"))


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "migrations").mkdir(parents=True)
    (root / "src" / "zhiheng").mkdir(parents=True)
    (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (root / "migrations" / "0001_initial.py").write_text("# initial\n", encoding="utf-8")
    (root / "src" / "zhiheng" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _reset_preparation_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(preparation, "_CACHE_ROOT", cache)
    monkeypatch.setattr(preparation, "_DATABASE_TEMPLATES", {})
    monkeypatch.setattr(preparation, "_RESTIC_TEMPLATES", {})
    return cache


def _install_fake_migrator(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    calls: Counter[str] = Counter()

    def upgrade(config: Config, revision: str) -> None:
        assert revision == "head"
        calls["upgrade"] += 1
        url = config.get_main_option("sqlalchemy.url")
        assert url is not None
        assert url.startswith("sqlite:///")
        db_path = Path(url.removeprefix("sqlite:///"))
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("CREATE TABLE prepared(value TEXT NOT NULL)")
            connection.execute("INSERT INTO prepared(value) VALUES (?)", (f"v{calls['upgrade']}",))
            connection.commit()

    monkeypatch.setattr(alembic_command, "upgrade", upgrade)
    return calls


def _value(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as connection:
        return str(connection.execute("SELECT value FROM prepared").fetchone()[0])


def test_template_lock_setup_failure_does_not_poison_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    destination = tmp_path / "template.sqlite"
    original_open = cast(Callable[..., int], os.open)
    failed = False

    def fail_once(path: str | bytes | int, flags: int, mode: int = 0o777) -> int:
        nonlocal failed
        if not failed and str(path).endswith(".template.sqlite.g006-lock"):
            failed = True
            raise OSError("synthetic lock setup failure")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_once)
    lock = preparation._exclusive_destination_lock(destination)
    with pytest.raises(OSError, match="synthetic lock setup failure"), lock:
        pass

    monkeypatch.setattr(os, "open", original_open)
    with preparation._exclusive_destination_lock(destination):
        pass


def test_shared_template_lock_setup_failure_does_not_poison_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    destination = tmp_path / "template.sqlite"
    original_flock = fcntl.flock
    failed = False

    def fail_once(descriptor: int, operation: int) -> None:
        nonlocal failed
        if not failed and operation == fcntl.LOCK_SH:
            failed = True
            raise OSError("synthetic shared lock setup failure")
        original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", fail_once)
    lock = preparation._shared_destination_lock(destination)
    with pytest.raises(OSError, match="synthetic shared lock setup failure"), lock:
        pass

    monkeypatch.setattr(fcntl, "flock", original_flock)
    with preparation._shared_destination_lock(destination):
        pass


@pytest.mark.parametrize("cleanup_failure", ["unlock", "close"])
def test_shared_template_lock_cleanup_failure_notifies_waiters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    destination = tmp_path / "template.sqlite"
    state = preparation._process_template_lock(destination)
    original_flock = fcntl.flock
    original_close = os.close
    failed = False

    def fail_unlock_once(descriptor: int, operation: int) -> None:
        nonlocal failed
        if not failed and operation == fcntl.LOCK_UN:
            failed = True
            raise OSError("synthetic shared unlock failure")
        original_flock(descriptor, operation)

    def fail_close_once(descriptor: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            original_close(descriptor)
            raise OSError("synthetic shared close failure")
        original_close(descriptor)

    def acquire_exclusive() -> None:
        with preparation._exclusive_destination_lock(destination):
            pass

    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    expected_error = f"synthetic shared {cleanup_failure} failure"
    try:
        with (
            pytest.raises(OSError, match=expected_error),
            preparation._shared_destination_lock(destination),
        ):
            future = executor.submit(acquire_exclusive)
            for _ in range(100):
                waiters = cast(Any, state.condition)._waiters
                if waiters:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("exclusive lock waiter did not block on shared lock")
            if cleanup_failure == "unlock":
                monkeypatch.setattr(fcntl, "flock", fail_unlock_once)
            else:
                monkeypatch.setattr(os, "close", fail_close_once)
        assert future is not None
        future.result(timeout=2)
    finally:
        monkeypatch.setattr(fcntl, "flock", original_flock)
        monkeypatch.setattr(os, "close", original_close)
        with state.condition:
            state.condition.notify_all()
        executor.shutdown(wait=True)


def test_migrated_database_reuses_private_copy_and_refuses_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)

    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"

    assert preparation.prepare_migrated_database(root, first) is False
    with closing(sqlite3.connect(first)) as connection:
        connection.execute("UPDATE prepared SET value = 'caller-write'")
        connection.commit()

    assert preparation.prepare_migrated_database(root, second) is True

    assert calls["upgrade"] == 1
    assert _value(first) == "caller-write"
    assert _value(second) == "v1"

    with pytest.raises(FileExistsError):
        preparation.prepare_migrated_database(root, second)
    assert _value(second) == "v1"


def test_migrated_database_rebuilds_corrupt_template_and_invalidates_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)

    assert preparation.prepare_migrated_database(root, tmp_path / "first.sqlite") is False
    template = next(iter(preparation._DATABASE_TEMPLATES.values()))
    template.path.write_bytes(b"not sqlite")

    assert preparation.prepare_migrated_database(root, tmp_path / "second.sqlite") is False
    assert _value(tmp_path / "second.sqlite") == "v2"

    (root / "migrations" / "0002_change.py").write_text("# schema change\n", encoding="utf-8")
    assert preparation.prepare_migrated_database(root, tmp_path / "third.sqlite") is False

    assert calls["upgrade"] == 3
    assert _value(tmp_path / "third.sqlite") == "v3"


def test_migrated_database_rebuilds_template_replaced_by_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    preparation.prepare_migrated_database(root, tmp_path / "first.sqlite")
    template = next(iter(preparation._DATABASE_TEMPLATES.values()))
    template.path.unlink()
    template.path.mkdir()

    preparation.prepare_migrated_database(root, tmp_path / "second.sqlite")
    assert calls["upgrade"] == 2
    assert _value(tmp_path / "second.sqlite") == "v2"


def test_migrated_database_rebuilds_valid_but_modified_template_without_process_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    preparation.prepare_migrated_database(root, tmp_path / "first.sqlite")
    template = next(iter(preparation._DATABASE_TEMPLATES.values()))
    with closing(sqlite3.connect(template.path)) as connection:
        connection.execute("UPDATE prepared SET value = 'tampered'")
        connection.commit()
    preparation._DATABASE_TEMPLATES.clear()

    assert preparation.prepare_migrated_database(root, tmp_path / "second.sqlite") is False
    assert _value(tmp_path / "second.sqlite") == "v2"
    assert calls["upgrade"] == 2


def test_migrated_database_preparation_is_thread_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)

    original_upgrade = alembic_command.upgrade

    def slow_upgrade(config: Config, revision: str) -> None:
        time.sleep(0.05)
        original_upgrade(config, revision)

    monkeypatch.setattr(alembic_command, "upgrade", slow_upgrade)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda index: preparation.prepare_migrated_database(
                    root,
                    tmp_path / f"thread-{index}.sqlite",
                ),
                range(8),
            )
        )

    assert calls["upgrade"] == 1
    assert results.count(False) == 1
    assert results.count(True) == 7
    assert {_value(tmp_path / f"thread-{index}.sqlite") for index in range(8)} == {"v1"}


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process preparation race requires fork support",
)
def test_database_same_target_cross_process_has_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    preparation.prepare_migrated_database(root, tmp_path / "template-seed.sqlite")
    target = tmp_path / "shared.sqlite"
    context = multiprocessing.get_context("fork")
    start = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_prepare_same_database_target_worker,
            args=(start, result_queue, root, target),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [result_queue.get(timeout=2) for _ in processes]
    assert [kind for kind, _ in results].count("ok") == 1
    assert [detail for kind, detail in results if kind == "error"] == ["FileExistsError"]
    assert _value(target) == "v1"
    assert calls["upgrade"] == 1


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process preparation race requires fork support",
)
def test_database_different_targets_cross_process_are_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    calls = _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    preparation.prepare_migrated_database(root, tmp_path / "template-seed.sqlite")
    targets = (tmp_path / "independent-a.sqlite", tmp_path / "independent-b.sqlite")
    context = multiprocessing.get_context("fork")
    start = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_prepare_same_database_target_worker,
            args=(start, result_queue, root, target),
        )
        for target in targets
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert [result_queue.get(timeout=2)[0] for _ in processes] == ["ok", "ok"]
    assert {_value(target) for target in targets} == {"v1"}
    assert calls["upgrade"] == 1


def test_database_copy_failure_leaves_no_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    template_destination = tmp_path / "first.sqlite"
    preparation.prepare_migrated_database(root, template_destination)
    target = tmp_path / "failed.sqlite"

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic copy interruption")

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="synthetic copy interruption"):
        preparation.prepare_migrated_database(root, target)

    assert not target.exists()
    assert not list(tmp_path.glob(".failed.sqlite.g006-*"))


def test_database_copy_validation_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    _install_fake_migrator(monkeypatch)
    root = _project_root(tmp_path)
    preparation.prepare_migrated_database(root, tmp_path / "template.sqlite")
    target = tmp_path / "validation.sqlite"
    original_digest = preparation._file_digest

    def mismatched_digest(path: Path) -> str:
        if path.name.startswith(".validation.sqlite.g006-"):
            return "synthetic-mismatch"
        return original_digest(path)

    monkeypatch.setattr(preparation, "_file_digest", mismatched_digest)
    with pytest.raises(RuntimeError, match="database copy failed integrity verification"):
        preparation.prepare_migrated_database(root, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".validation.sqlite.g006-*"))

    monkeypatch.setattr(preparation, "_file_digest", original_digest)
    assert preparation.prepare_migrated_database(root, target) is True
    assert target.is_file()


def _fake_restic_runner() -> tuple[Counter[str], Callable[[list[str], Path, dict[str, str]], str]]:
    calls: Counter[str] = Counter()

    def run(args: list[str], root: Path, env: dict[str, str]) -> str:
        del root
        command = args[1]
        calls[command] += 1
        if command == "version":
            return "restic 0.17.0 compiled with synthetic-test"
        repository = Path(env["RESTIC_REPOSITORY"])
        if command == "init":
            repository.mkdir(parents=True)
            (repository / "config").write_text("config", encoding="utf-8")
            (repository / "keys").mkdir()
            (repository / "keys" / "key").write_text("key", encoding="utf-8")
            return ""
        if command == "snapshots":
            return '[{"short_id":"dirty"}]' if (repository / "snapshots").exists() else "[]"
        raise AssertionError(args)

    return calls, run


def test_restic_repository_reuses_empty_private_copy_and_recovers_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    calls, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}

    first = tmp_path / "repo-a"
    second = tmp_path / "repo-b"
    third = tmp_path / "repo-c"

    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=first,
            project_root=root,
            environment=env,
            run=run,
        )
        is False
    )
    (first / "snapshots").mkdir()

    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=second,
            project_root=root,
            environment=env,
            run=run,
        )
        is True
    )
    assert not (second / "snapshots").exists()

    template = next(iter(preparation._RESTIC_TEMPLATES.values()))
    (template.path / "config").write_text("corrupt", encoding="utf-8")
    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=third,
            project_root=root,
            environment=env,
            run=run,
        )
        is False
    )

    assert calls["init"] == 2
    assert calls["snapshots"] == 2
    assert calls["version"] == 3
    assert (third / "config").read_text(encoding="utf-8") == "config"

    with pytest.raises(FileExistsError):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=third,
            project_root=root,
            environment=env,
            run=run,
        )
    assert (third / "config").read_text(encoding="utf-8") == "config"


def test_restic_repository_rebuilds_corrupt_template_without_process_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    calls, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}

    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "repo-a",
        project_root=root,
        environment=env,
        run=run,
    )
    template = next(iter(preparation._RESTIC_TEMPLATES.values()))
    (template.path / "config").write_text("corrupt", encoding="utf-8")
    preparation._RESTIC_TEMPLATES.clear()

    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=tmp_path / "repo-b",
            project_root=root,
            environment=env,
            run=run,
        )
        is False
    )
    assert (tmp_path / "repo-b" / "config").read_text(encoding="utf-8") == "config"
    assert calls["init"] == 2


def test_restic_template_probe_failure_is_not_silently_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    calls, successful_run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "repo-a",
        project_root=root,
        environment=env,
        run=successful_run,
    )
    preparation._RESTIC_TEMPLATES.clear()

    def probe_fails_once(args: list[str], root: Path, env: dict[str, str]) -> str:
        if args[1] == "snapshots" and calls["snapshots"] == 1:
            calls["snapshots"] += 1
            raise RuntimeError("synthetic snapshots probe failure")
        return successful_run(args, root, env)

    with pytest.raises(RuntimeError, match="synthetic snapshots probe failure"):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=tmp_path / "repo-b",
            project_root=root,
            environment=env,
            run=probe_fails_once,
        )

    assert calls["init"] == 1
    assert not (tmp_path / "repo-b").exists()
    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=tmp_path / "repo-c",
            project_root=root,
            environment=env,
            run=successful_run,
        )
        is True
    )
    assert calls["init"] == 1
    assert (tmp_path / "repo-c" / "config").read_text(encoding="utf-8") == "config"


def test_restic_different_targets_copy_in_parallel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    _, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "seed-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    original_install = preparation._install_restic_copy
    first_entered = threading.Event()
    both_entered = threading.Event()
    release = threading.Event()
    entered = 0
    entered_lock = threading.Lock()

    def blocking_install(template: Path, destination: Path, digest: str) -> None:
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 1:
                first_entered.set()
            elif entered == 2:
                both_entered.set()
        assert release.wait(2)
        original_install(template, destination, digest)

    monkeypatch.setattr(preparation, "_install_restic_copy", blocking_install)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                preparation.prepare_restic_repository,
                binary="restic",
                repository=tmp_path / f"parallel-{index}",
                project_root=root,
                environment=env,
                run=run,
            )
            for index in range(2)
        ]
        assert first_entered.wait(2)
        parallel = both_entered.wait(2)
        release.set()
        assert [future.result() for future in futures] == [True, True]

    assert parallel


def test_restic_rebuilds_template_replaced_by_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    calls, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "first-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    template = next(iter(preparation._RESTIC_TEMPLATES.values()))
    shutil.rmtree(template.path)
    template.path.write_text("wrong template shape", encoding="utf-8")

    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "second-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    assert calls["init"] == 2
    assert (tmp_path / "second-repo" / "config").read_text(encoding="utf-8") == "config"


def test_restic_repository_failed_initialization_does_not_poison_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    calls, successful_run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}

    def failing_once(args: list[str], root: Path, env: dict[str, str]) -> str:
        if args[1] == "init" and calls["init"] == 0:
            calls["init"] += 1
            raise RuntimeError("synthetic init failure")
        return successful_run(args, root, env)

    with pytest.raises(RuntimeError, match="synthetic init failure"):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=tmp_path / "failed",
            project_root=root,
            environment=env,
            run=failing_once,
        )

    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=tmp_path / "recovered",
            project_root=root,
            environment=env,
            run=failing_once,
        )
        is False
    )
    assert calls["init"] == 2
    assert (tmp_path / "recovered" / "config").exists()


def test_restic_copy_failure_cleans_lock_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    _, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "first-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    target = tmp_path / "failed-repo"

    def fail_copytree(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic directory copy interruption")

    monkeypatch.setattr(shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="synthetic directory copy interruption"):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=target,
            project_root=root,
            environment=env,
            run=run,
        )

    assert not target.exists()
    assert (tmp_path / ".failed-repo.g006-lock").exists()
    assert not [
        path
        for path in tmp_path.glob(".failed-repo.g006-*")
        if path.name != ".failed-repo.g006-lock"
    ]


def test_restic_copy_validation_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    _, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "first-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    target = tmp_path / "validation-repo"
    original_digest = preparation._directory_digest

    def mismatched_digest(path: Path) -> str:
        if path.name.startswith(".validation-repo.g006-"):
            return "synthetic-mismatch"
        return original_digest(path)

    monkeypatch.setattr(preparation, "_directory_digest", mismatched_digest)
    with pytest.raises(RuntimeError, match="repository copy failed integrity verification"):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=target,
            project_root=root,
            environment=env,
            run=run,
        )
    assert not target.exists()
    assert not [
        path
        for path in tmp_path.glob(".validation-repo.g006-*")
        if path.name != ".validation-repo.g006-lock"
    ]

    monkeypatch.setattr(preparation, "_directory_digest", original_digest)
    assert (
        preparation.prepare_restic_repository(
            binary="restic",
            repository=target,
            project_root=root,
            environment=env,
            run=run,
        )
        is True
    )
    assert (target / "config").read_text(encoding="utf-8") == "config"


def test_restic_copy_cleanup_failure_is_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    _, run = _fake_restic_runner()
    env = {"RESTIC_PASSWORD": "protected-synthetic-backup-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "first-repo",
        project_root=root,
        environment=env,
        run=run,
    )
    target = tmp_path / "diagnostic-repo"
    original_rmtree = shutil.rmtree

    def fail_copytree(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic directory copy interruption")

    def fail_staging_cleanup(path: str | Path, ignore_errors: bool = False, **kwargs: Any) -> None:
        if Path(path).name.startswith(".diagnostic-repo.g006-"):
            raise OSError("synthetic cleanup interruption")
        original_rmtree(path, ignore_errors=ignore_errors, **kwargs)

    monkeypatch.setattr(shutil, "copytree", fail_copytree)
    monkeypatch.setattr(shutil, "rmtree", fail_staging_cleanup)
    with pytest.raises(RuntimeError, match="failed to clean restic preparation staging"):
        preparation.prepare_restic_repository(
            binary="restic",
            repository=target,
            project_root=root,
            environment=env,
            run=run,
        )


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process preparation race requires fork support",
)
def test_restic_same_target_cross_process_has_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    env = {"RESTIC_PASSWORD": "protected-multiprocess-password"}
    preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "template-seed",
        project_root=root,
        environment=env,
        run=_multiprocess_fake_restic_run,
    )
    target = tmp_path / "shared-repo"
    context = multiprocessing.get_context("fork")
    start = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_prepare_same_restic_target_worker,
            args=(start, result_queue, root, target),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [result_queue.get(timeout=2) for _ in processes]
    assert [kind for kind, _ in results].count("ok") == 1
    assert [detail for kind, detail in results if kind == "error"] == ["FileExistsError"]
    assert (target / "config").read_text(encoding="utf-8") == "config"
    assert (tmp_path / ".shared-repo.g006-lock").exists()


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process preparation race requires fork support",
)
def test_restic_different_targets_cross_process_are_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_preparation_cache(monkeypatch, tmp_path)
    root = _project_root(tmp_path)
    targets = (tmp_path / "independent-a", tmp_path / "independent-b")
    context = multiprocessing.get_context("fork")
    start = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_prepare_same_restic_target_worker,
            args=(start, result_queue, root, target),
        )
        for target in targets
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert [result_queue.get(timeout=2)[0] for _ in processes] == ["ok", "ok"]
    assert all((target / "config").read_text(encoding="utf-8") == "config" for target in targets)
