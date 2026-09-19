"""Add stable topic domains, record types, and approval-backed taxonomy proposals."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_topic_taxonomy"
down_revision: str | None = "0031_personal_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


PRIMARY_DOMAINS = (
    ("mathematics_formal_sciences", "数学与形式科学", "数学、逻辑、概率与统计基础", 10),
    ("natural_sciences", "自然科学", "物理、化学、生物、地球与宇宙科学", 20),
    ("computing_engineering", "计算机与工程技术", "计算机、AI、软件、电子及其他工程", 30),
    ("medicine_health", "医学与健康", "医学、营养、运动、睡眠与心理健康", 40),
    ("psychology_cognition", "心理与认知", "感知、情绪、动机、认知及行为机制", 50),
    ("society_politics_law", "社会、政治与法律", "社会结构、公共政策、政治、法律与制度", 60),
    ("economics_finance_business", "经济、金融与商业", "经济学、投资、个人财务与商业管理", 70),
    ("history_philosophy_religion", "历史、哲学与宗教", "历史解释、哲学思想、伦理与宗教", 80),
    ("language_literature_arts", "语言、文学与艺术", "语言、文学、音乐、视觉艺术与创作", 90),
    ("education_learning", "教育与学习", "教育方法、学习策略、知识管理与技能习得", 100),
    ("career_work_practice", "职业与工作实践", "职业选择、求职、工作协作与个人工作方法", 110),
    ("relationships_communication", "人际关系与沟通", "亲密关系、家庭、社交、沟通与冲突处理", 120),
    ("lifestyle_daily_life", "生活方式与日常事务", "居家、穿搭、饮食、出行与日常安排", 130),
    ("sports_games_leisure", "体育、游戏与休闲", "运动项目、竞技、游戏规则与休闲活动", 140),
)


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(knowledge_objects)")
    }
    if "record_type" not in columns:
        op.add_column(
            "knowledge_objects",
            sa.Column("record_type", sa.String(64), nullable=False, server_default="knowledge"),
        )
    if "classification_revision" not in columns:
        op.add_column(
            "knowledge_objects",
            sa.Column("classification_revision", sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_table(
        "record_types",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )
    op.create_table(
        "taxonomy_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("proposal_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("preview_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by_user_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_taxonomy_proposals_owner_status",
        "taxonomy_proposals",
        ["owner_user_id", "status", "created_at"],
    )
    op.create_table(
        "taxonomy_proposal_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["taxonomy_proposals.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"]),
    )
    op.create_index(
        "ix_taxonomy_proposal_events_proposal",
        "taxonomy_proposal_events",
        ["proposal_id", "created_at"],
    )
    op.execute(
        """
        INSERT OR IGNORE INTO record_types (id, name, description, status)
        VALUES
          ('knowledge', '知识条目', '可独立理解的主题知识', 'active'),
          ('personal_archive_experience', '个人档案与经历',
           '个人经历、决策、反思与结果的记录类型', 'active')
        """
    )
    for domain_id, name, description, sort_order in PRIMARY_DOMAINS:
        op.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO domain_catalog
                  (id, name, description, sort_order, schema_version, status)
                VALUES (:id, :name, :description, :sort_order, 2, 'active')
                """
            ).bindparams(
                id=domain_id, name=name, description=description, sort_order=sort_order
            )
        )


def downgrade() -> None:
    op.drop_index("ix_taxonomy_proposal_events_proposal", table_name="taxonomy_proposal_events")
    op.drop_table("taxonomy_proposal_events")
    op.drop_index("ix_taxonomy_proposals_owner_status", table_name="taxonomy_proposals")
    op.drop_table("taxonomy_proposals")
    op.drop_table("record_types")
    # SQLite refuses ALTER TABLE DROP COLUMN while older compatibility views
    # reference columns from later migrations. Retain these defaults on
    # downgrade; they are inert once taxonomy tables are removed.
