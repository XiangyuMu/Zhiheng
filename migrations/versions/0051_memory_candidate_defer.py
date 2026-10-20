"""Let memory candidates be deferred and restored (GLOSSARY 延后, ADR 0011, issue #78).

`memory_candidates.status` gains `deferred`. SQLite cannot alter a CHECK constraint,
so the table is rebuilt: the stored CREATE statement is reused with the widened
status list, rows are copied, and the table's own index and triggers are recreated
from their stored SQL. Child tables reference the table by name, so foreign keys are
deferred to commit and the rename runs in legacy mode to leave other tables'
references and trigger bodies untouched.

`memory_candidate_events` records each defer and restore so the candidate history
shows them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0051_memory_candidate_defer"
down_revision: str | Sequence[str] | None = "0050_unify_domain_catalog"
branch_labels = None
depends_on = None

_OLD_STATUSES = (
    "'pending_confirmation', 'edited', 'confirmed', 'rejected', 'superseded', 'privacy_erased'"
)
_REBUILT = "memory_candidates_0051"
_NEW_STATUSES = (
    "'pending_confirmation', 'edited', 'confirmed', 'rejected', 'superseded', "
    "'privacy_erased', 'deferred'"
)


def upgrade() -> None:
    _rebuild_candidates(_OLD_STATUSES, _NEW_STATUSES)
    op.create_table(
        "memory_candidate_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("candidate_version_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("operation_key", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["memory_candidates.id"]),
        sa.CheckConstraint(
            "event_type in ('deferred', 'restored')", name="ck_memory_candidate_events_type"
        ),
        sa.UniqueConstraint("operation_key", name="uq_memory_candidate_events_operation_key"),
    )
    op.create_index(
        "ix_memory_candidate_events_candidate",
        "memory_candidate_events",
        ["candidate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_candidate_events_candidate", table_name="memory_candidate_events")
    op.drop_table("memory_candidate_events")
    # Deferral is not a decision, so a deferred candidate is pending again without it.
    op.execute(
        "UPDATE memory_candidates SET status = 'pending_confirmation', "
        "updated_at = CURRENT_TIMESTAMP WHERE status = 'deferred'"
    )
    _rebuild_candidates(_NEW_STATUSES, _OLD_STATUSES)


def _rebuild_candidates(old_statuses: str, new_statuses: str) -> None:
    bind = op.get_bind()
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_candidates'")
    ).scalar_one()
    old_check = f"status in ({old_statuses})"
    if old_check not in table_sql:
        raise RuntimeError("memory_candidates status constraint has an unexpected definition")
    dependents = [
        str(row[0])
        for row in bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE tbl_name = 'memory_candidates' AND type IN ('index', 'trigger') "
                "AND sql IS NOT NULL ORDER BY type, name"
            )
        )
    ]
    columns = ", ".join(
        str(row[1]) for row in bind.execute(sa.text("PRAGMA table_info(memory_candidates)"))
    )
    # foreign_keys can only change outside a transaction; this is SQLite's documented
    # table-rebuild procedure, run in its own transaction with enforcement off.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text("PRAGMA foreign_keys = OFF"))
        bind.execute(sa.text("PRAGMA legacy_alter_table = ON"))
        try:
            bind.execute(sa.text("BEGIN"))
            try:
                bind.execute(
                    sa.text(
                        table_sql.replace("memory_candidates", _REBUILT, 1).replace(
                            old_check, f"status in ({new_statuses})"
                        )
                    )
                )
                bind.execute(
                    sa.text(
                        f"INSERT INTO {_REBUILT} ({columns}) "
                        f"SELECT {columns} FROM memory_candidates"
                    )
                )
                bind.execute(sa.text("DROP TABLE memory_candidates"))
                bind.execute(sa.text(f"ALTER TABLE {_REBUILT} RENAME TO memory_candidates"))
                for statement in dependents:
                    bind.execute(sa.text(statement))
                violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
                if violations:
                    raise RuntimeError(
                        f"memory_candidates rebuild left foreign key violations: {violations}"
                    )
            except BaseException:
                bind.execute(sa.text("ROLLBACK"))
                raise
            bind.execute(sa.text("COMMIT"))
        finally:
            bind.execute(sa.text("PRAGMA legacy_alter_table = OFF"))
            bind.execute(sa.text("PRAGMA foreign_keys = ON"))
