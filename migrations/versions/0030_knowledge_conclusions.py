"""Versioned conversation conclusions and their approval authority."""
from alembic import op

revision = '0030_knowledge_conclusions'
down_revision = '0029_event_chunk_hash'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('''CREATE TABLE conclusion_sources (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, body TEXT NOT NULL,
      history_id TEXT REFERENCES answer_history(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    op.execute('''CREATE TABLE conclusion_entries (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
      source_id TEXT NOT NULL REFERENCES conclusion_sources(id) ON DELETE CASCADE,
      current_version INTEGER NOT NULL, approved_version INTEGER,
      status TEXT NOT NULL DEFAULT 'draft', knowledge_id TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    op.execute('''CREATE TABLE conclusion_versions (
      entry_id TEXT NOT NULL REFERENCES conclusion_entries(id) ON DELETE CASCADE,
      version INTEGER NOT NULL, payload_json TEXT NOT NULL,
      approved_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(entry_id,version))''')
    op.execute('''CREATE TABLE conclusion_operations (
      owner_user_id TEXT NOT NULL, operation_key TEXT NOT NULL,
      request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
      PRIMARY KEY(owner_user_id,operation_key))''')
    op.execute('''CREATE TABLE conclusion_relations (
      id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
      left_id TEXT NOT NULL REFERENCES conclusion_entries(id) ON DELETE CASCADE,
      right_id TEXT NOT NULL REFERENCES conclusion_entries(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
      left_version INTEGER NOT NULL, right_version INTEGER NOT NULL)''')
    op.execute('''CREATE TRIGGER conclusion_source_delete BEFORE DELETE ON conclusion_sources
      BEGIN
        UPDATE knowledge_objects SET lifecycle_status='soft_deleted'
        WHERE id IN (SELECT knowledge_id FROM conclusion_entries WHERE source_id=OLD.id);
        DELETE FROM conclusion_operations WHERE owner_user_id=OLD.owner_user_id;
      END''')
    op.execute('DROP VIEW current_formal_knowledge')
    op.execute('''CREATE VIEW current_formal_knowledge AS
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
            AND julianday(json_extract(v.payload_json,'$.valid_until'))<=julianday('now'))))
    ''')


def downgrade() -> None:
    op.execute('DROP VIEW current_formal_knowledge')
    op.execute('''CREATE VIEW current_formal_knowledge AS
      SELECT ko.*, kv.id AS version_id, kv.version_no, kv.summary, kv.markdown_uri,
             kv.content_version_id, cv.evidence_object_id, cv.status AS content_status
      FROM knowledge_objects ko JOIN knowledge_versions kv ON kv.id=ko.current_version_id
      LEFT JOIN content_versions cv ON cv.id=kv.content_version_id
      WHERE ko.lifecycle_status='formal_current' AND ko.visibility_scope='formal' ''')
    op.execute('DROP TRIGGER conclusion_source_delete')
    for name in ('conclusion_relations','conclusion_operations','conclusion_versions',
                 'conclusion_entries','conclusion_sources'):
        op.execute(f'DROP TABLE {name}')
