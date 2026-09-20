"""Track conclusion applicability separately from approval and immutable versions."""

from alembic import op

revision = "0034_conclusion_applicability"
down_revision = "0033_conclusion_relation_history"
branch_labels = None
depends_on = None


def _view(*, applicability: bool) -> None:
    extra = """
          OR EXISTS (SELECT 1 FROM conclusion_applicability a
                     WHERE a.entry_id=e.id AND a.version=e.approved_version
                       AND a.state='suspended')
          OR EXISTS (SELECT 1 FROM json_each(v.payload_json,'$.premises') p
                     WHERE json_extract(p.value,'$.confirmed')=1
                       AND julianday(json_extract(p.value,'$.valid_until'))<=julianday('now'))
    """ if applicability else ""
    op.execute("DROP VIEW current_formal_knowledge")
    op.execute("""CREATE VIEW current_formal_knowledge AS
      SELECT ko.*, kv.id AS version_id, kv.version_no, kv.summary, kv.markdown_uri,
             kv.content_version_id, cv.evidence_object_id, cv.status AS content_status
      FROM knowledge_objects ko JOIN knowledge_versions kv ON kv.id=ko.current_version_id
      LEFT JOIN content_versions cv ON cv.id=kv.content_version_id
      WHERE ko.lifecycle_status='formal_current' AND ko.visibility_scope='formal'
      AND NOT EXISTS (
        SELECT 1 FROM conclusion_entries e
        JOIN conclusion_versions v ON v.entry_id=e.id AND v.version=e.approved_version
        WHERE e.knowledge_id=ko.id AND (e.status!='formal'
          OR (json_extract(v.payload_json,'$.valid_until') IS NOT NULL
            AND julianday(json_extract(v.payload_json,'$.valid_until'))<=julianday('now'))
    """ + extra + "))")


def upgrade() -> None:
    op.execute("""CREATE TABLE conclusion_applicability (
        entry_id TEXT PRIMARY KEY REFERENCES conclusion_entries(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('suspended','review_required')),
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    op.execute("""CREATE TABLE conclusion_applicability_events (
        id TEXT PRIMARY KEY,
        entry_id TEXT NOT NULL REFERENCES conclusion_entries(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('suspended','review_required')),
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        event_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    _view(applicability=True)


def downgrade() -> None:
    _view(applicability=False)
    op.execute("DROP TABLE conclusion_applicability_events")
    op.execute("DROP TABLE conclusion_applicability")
