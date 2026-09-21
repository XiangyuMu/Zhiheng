"""Keep approved same-premise conflicts out of unconditional serving."""
from alembic import op

revision = "0035_conclusion_conflict_serving"
down_revision = "0034_conclusion_applicability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='current_formal_knowledge'"
    ).scalar_one()
    op.execute("DROP VIEW current_formal_knowledge")
    op.execute(definition + """ AND NOT EXISTS (
        SELECT 1 FROM conclusion_entries e
        JOIN conclusion_relations r ON e.id IN (r.left_id,r.right_id)
        JOIN conclusion_entries le ON le.id=r.left_id
        JOIN conclusion_entries re ON re.id=r.right_id
        WHERE e.knowledge_id=ko.id AND r.kind='conflict' AND r.status='approved'
          AND le.status='formal' AND re.status='formal'
    )""")


def downgrade() -> None:
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
          OR EXISTS (SELECT 1 FROM conclusion_applicability a
                     WHERE a.entry_id=e.id AND a.version=e.approved_version
                       AND a.state='suspended')
          OR EXISTS (SELECT 1 FROM json_each(v.payload_json,'$.premises') p
                     WHERE json_extract(p.value,'$.confirmed')=1
                       AND julianday(json_extract(p.value,'$.valid_until'))<=julianday('now'))
        ))""")
