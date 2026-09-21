"""Productize knowledge classification taxonomy and audit history."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_classification_productization"
down_revision: str | None = "0014_knowledge_import_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


DOMAIN_CATALOG = (
    (
        "academic_career",
        "Academic and Career",
        "Research planning, papers, applications and professional positioning",
        10,
    ),
    (
        "technology_engineering",
        "Technology and Engineering",
        "Programming, systems, AI engineering and software architecture",
        20,
    ),
    (
        "finance_assets",
        "Finance and Assets",
        "Financial literacy, investing, budgeting and risk frameworks",
        30,
    ),
    (
        "society_public_issues",
        "Society and Public Issues",
        "News, policy, institutions and public debates",
        40,
    ),
    (
        "learning_personal_development",
        "Learning and Personal Development",
        "Learning plans, metacognition, habits and skill acquisition",
        50,
    ),
    (
        "health_wellbeing",
        "Health and Wellbeing",
        "Physical health, mental wellbeing, sleep and exercise",
        60,
    ),
    (
        "relationships_communication",
        "Relationships and Communication",
        "Conversation, emotional communication and conflict handling",
        70,
    ),
    (
        "lifestyle_aesthetics",
        "Lifestyle and Aesthetics",
        "Clothing, photography taste, home and life choices",
        80,
    ),
    (
        "arts_creation",
        "Arts and Creation",
        "Writing, photography, visual creation, music, film and art study",
        90,
    ),
    (
        "personal_archive_experience",
        "Personal Archive and Experience",
        "Personal decisions, reflections, events and project history",
        100,
    ),
)


def upgrade() -> None:
    op.create_table(
        "domain_catalog",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )
    op.create_table(
        "classification_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("domain_id", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
        sa.ForeignKeyConstraint(["domain_id"], ["domain_catalog.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["classification_nodes.id"]),
        sa.UniqueConstraint(
            "owner_user_id", "parent_id", "name", name="uq_classification_nodes_parent_name"
        ),
        sa.CheckConstraint("level IN (2, 3)", name="ck_classification_nodes_level"),
    )
    op.create_index(
        "ix_classification_nodes_owner_domain",
        "classification_nodes",
        ["owner_user_id", "domain_id", "status", "sort_order"],
    )
    op.create_table(
        "knowledge_classifications",
        sa.Column("knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("classification_node_id", sa.String(36), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(32), nullable=False, server_default="user_confirmed"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confirmation_status", sa.String(32), nullable=False, server_default="confirmed"),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["classification_node_id"], ["classification_nodes.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["auth_users.id"]),
        sa.PrimaryKeyConstraint("knowledge_object_id", "classification_node_id"),
    )
    op.create_index(
        "ix_knowledge_classifications_node",
        "knowledge_classifications",
        ["classification_node_id", "confirmation_status"],
    )
    op.create_table(
        "classification_change_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("after_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source", sa.String(32), nullable=False, server_default="user"),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_classification_change_events_knowledge_created",
        "classification_change_events",
        ["knowledge_object_id", "created_at", "id"],
    )
    op.create_table(
        "classification_suggestions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("knowledge_object_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_classification_suggestions_owner_status",
        "classification_suggestions",
        ["owner_user_id", "status", "created_at"],
    )

    op.bulk_insert(
        sa.table(
            "domain_catalog",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.Text),
            sa.column("sort_order", sa.Integer),
            sa.column("schema_version", sa.Integer),
            sa.column("status", sa.String),
        ),
        [
            {
                "id": domain_id,
                "name": name,
                "description": description,
                "sort_order": sort_order,
                "schema_version": 1,
                "status": "active",
            }
            for domain_id, name, description, sort_order in DOMAIN_CATALOG
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_suggestions_owner_status", table_name="classification_suggestions"
    )
    op.drop_table("classification_suggestions")
    op.drop_index(
        "ix_classification_change_events_knowledge_created",
        table_name="classification_change_events",
    )
    op.drop_table("classification_change_events")
    op.drop_index("ix_knowledge_classifications_node", table_name="knowledge_classifications")
    op.drop_table("knowledge_classifications")
    op.drop_index("ix_classification_nodes_owner_domain", table_name="classification_nodes")
    op.drop_table("classification_nodes")
    op.drop_table("domain_catalog")
