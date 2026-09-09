from __future__ import annotations

from zhiheng.core.config import Settings
from zhiheng.db.session import create_sqlite_engine, sqlite_pragmas


def test_sqlite_engine_enables_required_pragmas(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "zhiheng.db"
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        sqlite_busy_timeout_ms=7000,
    )
    engine = create_sqlite_engine(settings)

    pragmas = sqlite_pragmas(engine)

    assert pragmas["foreign_keys"] == 1
    assert pragmas["busy_timeout"] == 7000
    assert str(pragmas["journal_mode"]).lower() == "wal"
