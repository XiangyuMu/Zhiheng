from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config

from zhiheng.evaluation import g006_preparation as preparation


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
            return "[{\"short_id\":\"dirty\"}]" if (repository / "snapshots").exists() else "[]"
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

    assert preparation.prepare_restic_repository(
        binary="restic",
        repository=first,
        project_root=root,
        environment=env,
        run=run,
    ) is False
    (first / "snapshots").mkdir()

    assert preparation.prepare_restic_repository(
        binary="restic",
        repository=second,
        project_root=root,
        environment=env,
        run=run,
    ) is True
    assert not (second / "snapshots").exists()

    template = next(iter(preparation._RESTIC_TEMPLATES.values()))
    (template.path / "config").write_text("corrupt", encoding="utf-8")
    assert preparation.prepare_restic_repository(
        binary="restic",
        repository=third,
        project_root=root,
        environment=env,
        run=run,
    ) is False

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

    assert preparation.prepare_restic_repository(
        binary="restic",
        repository=tmp_path / "recovered",
        project_root=root,
        environment=env,
        run=failing_once,
    ) is False
    assert calls["init"] == 2
    assert (tmp_path / "recovered" / "config").exists()
