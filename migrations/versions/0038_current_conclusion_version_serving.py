"""Serve only the conclusion that authored a knowledge object's current version."""

from __future__ import annotations

from alembic import op

revision = "0038_current_conclusion_version_serving"
down_revision = "0037_conversation_extraction_review"
branch_labels = None
depends_on = None


def _create_view() -> None:
    op.execute(
        """
        CREATE VIEW current_formal_knowledge AS
          SELECT ko.*, kv.id AS version_id, kv.version_no, kv.summary, kv.markdown_uri,
                 kv.content_version_id, cv.evidence_object_id, cv.status AS content_status
          FROM knowledge_objects ko
          JOIN knowledge_versions kv ON kv.id=ko.current_version_id
          LEFT JOIN content_versions cv ON cv.id=kv.content_version_id
          LEFT JOIN evidence_objects eo ON eo.id=cv.evidence_object_id
          WHERE ko.lifecycle_status='formal_current' AND ko.visibility_scope='formal'
          AND (
            (
              json_extract(eo.source_metadata_json, '$.conclusion_entry_id') IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM conclusion_entries current_entry
                JOIN conclusion_versions current_version
                  ON current_version.entry_id=current_entry.id
                 AND current_version.version=current_entry.approved_version
                WHERE current_entry.id=json_extract(
                        eo.source_metadata_json, '$.conclusion_entry_id'
                      )
                  AND current_entry.owner_user_id=ko.owner_user_id
                  AND current_entry.knowledge_id=ko.id
                  AND current_entry.status='formal'
                  AND (
                    json_extract(current_version.payload_json, '$.valid_until') IS NULL
                    OR julianday(json_extract(
                         current_version.payload_json, '$.valid_until'
                       )) > julianday('now')
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM conclusion_applicability a
                    WHERE a.entry_id=current_entry.id
                      AND a.version=current_entry.approved_version
                      AND a.state='suspended'
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM json_each(current_version.payload_json, '$.premises') p
                    WHERE json_extract(p.value, '$.confirmed')=1
                      AND julianday(json_extract(p.value, '$.valid_until'))
                          <= julianday('now')
                  )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM conclusion_relations r
                JOIN conclusion_entries left_entry
                  ON left_entry.id=r.left_id
                 AND left_entry.owner_user_id=r.owner_user_id
                JOIN conclusion_entries right_entry
                  ON right_entry.id=r.right_id
                 AND right_entry.owner_user_id=r.owner_user_id
                WHERE r.owner_user_id=ko.owner_user_id
                  AND r.status='approved'
                  AND r.kind='conflict'
                  AND (
                    r.left_id=json_extract(eo.source_metadata_json, '$.conclusion_entry_id')
                    OR r.right_id=json_extract(eo.source_metadata_json, '$.conclusion_entry_id')
                  )
                  AND left_entry.status='formal'
                  AND right_entry.status='formal'
              )
            )
            OR (
              json_extract(eo.source_metadata_json, '$.conclusion_entry_id') IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM conclusion_entries e
                JOIN conclusion_versions v
                  ON v.entry_id=e.id AND v.version=e.approved_version
                WHERE e.knowledge_id=ko.id
                  AND (
                    e.status!='formal'
                    OR (
                      json_extract(v.payload_json, '$.valid_until') IS NOT NULL
                      AND julianday(json_extract(v.payload_json, '$.valid_until'))
                          <= julianday('now')
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM conclusion_applicability a
                      WHERE a.entry_id=e.id
                        AND a.version=e.approved_version
                        AND a.state='suspended'
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM json_each(v.payload_json, '$.premises') p
                      WHERE json_extract(p.value, '$.confirmed')=1
                        AND julianday(json_extract(p.value, '$.valid_until'))
                            <= julianday('now')
                    )
                  )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM conclusion_entries e
                JOIN conclusion_relations r ON e.id IN (r.left_id,r.right_id)
                JOIN conclusion_entries left_entry ON left_entry.id=r.left_id
                JOIN conclusion_entries right_entry ON right_entry.id=r.right_id
                WHERE e.knowledge_id=ko.id
                  AND r.kind='conflict'
                  AND r.status='approved'
                  AND left_entry.status='formal'
                  AND right_entry.status='formal'
              )
            )
          )
        """
    )


def upgrade() -> None:
    op.execute("DROP VIEW current_formal_knowledge")
    _create_view()


def downgrade() -> None:
    op.execute("DROP VIEW current_formal_knowledge")
    op.execute(
        """
        CREATE VIEW current_formal_knowledge AS
          SELECT ko.*, kv.id AS version_id, kv.version_no, kv.summary, kv.markdown_uri,
                 kv.content_version_id, cv.evidence_object_id, cv.status AS content_status
          FROM knowledge_objects ko
          JOIN knowledge_versions kv ON kv.id=ko.current_version_id
          LEFT JOIN content_versions cv ON cv.id=kv.content_version_id
          WHERE ko.lifecycle_status='formal_current' AND ko.visibility_scope='formal'
          AND NOT EXISTS (
            SELECT 1
            FROM conclusion_entries e
            JOIN conclusion_versions v
              ON v.entry_id=e.id AND v.version=e.approved_version
            WHERE e.knowledge_id=ko.id
              AND (
                e.status!='formal'
                OR (
                  json_extract(v.payload_json, '$.valid_until') IS NOT NULL
                  AND julianday(json_extract(v.payload_json, '$.valid_until'))
                      <= julianday('now')
                )
                OR EXISTS (
                  SELECT 1
                  FROM conclusion_applicability a
                  WHERE a.entry_id=e.id
                    AND a.version=e.approved_version
                    AND a.state='suspended'
                )
                OR EXISTS (
                  SELECT 1
                  FROM json_each(v.payload_json, '$.premises') p
                  WHERE json_extract(p.value, '$.confirmed')=1
                    AND julianday(json_extract(p.value, '$.valid_until'))
                        <= julianday('now')
                )
              )
          )
          AND NOT EXISTS (
            SELECT 1
            FROM conclusion_entries e
            JOIN conclusion_relations r ON e.id IN (r.left_id,r.right_id)
            JOIN conclusion_entries left_entry ON left_entry.id=r.left_id
            JOIN conclusion_entries right_entry ON right_entry.id=r.right_id
            WHERE e.knowledge_id=ko.id
              AND r.kind='conflict'
              AND r.status='approved'
              AND left_entry.status='formal'
              AND right_entry.status='formal'
          )
        """
    )
