from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, Engine, event, pool
from sqlalchemy.engine import engine_from_config

from zhiheng.db.base import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if isinstance(connectable, Engine) and connectable.url.get_backend_name() == "sqlite":
        event.listen(connectable, "connect", _set_sqlite_pragmas)

    with connectable.connect() as connection:
        _configure_context(connection)
        with context.begin_transaction():
            context.run_migrations()


def _configure_context(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
