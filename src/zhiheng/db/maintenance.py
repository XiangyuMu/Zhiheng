from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Any


def acquire_database_lock(database: str, *, exclusive: bool) -> int:
    """Coordinate serving and replacement. Never unlink the stable lock inode."""
    path = Path(database).expanduser().resolve()
    descriptor = os.open(f"{path}.maintenance.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class ServingSQLiteConnection(sqlite3.Connection):
    """Raw Worker connections obey the same lock protocol as pooled sessions."""

    def __init__(self, database: str, *args: Any, **kwargs: Any) -> None:
        self._maintenance_descriptor: int | None = None
        descriptor = (
            acquire_database_lock(database, exclusive=False)
            if database and database != ":memory:"
            else None
        )
        try:
            super().__init__(database, *args, **kwargs)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        self._maintenance_descriptor = descriptor

    def close(self) -> None:
        try:
            super().close()
        finally:
            descriptor = self._maintenance_descriptor
            self._maintenance_descriptor = None
            if descriptor is not None:
                os.close(descriptor)

    def __del__(self) -> None:
        descriptor = self._maintenance_descriptor
        if descriptor is not None:
            self.close()
