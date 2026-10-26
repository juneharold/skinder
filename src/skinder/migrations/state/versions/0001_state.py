"""Create personal profiles, matching sessions, feedback, and bookmarks."""

import sqlalchemy as sa
from alembic import op

revision = "0001_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.CheckConstraint("id=1"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("recommendations", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "session_skills",
        sa.Column("session_id", sa.Text(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("skill_id", sa.Text(), primary_key=True),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), sa.ForeignKey("sessions.id")),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("session_id", "skill_id"),
    )
    op.create_table(
        "bookmarks",
        sa.Column("skill_id", sa.Text(), primary_key=True),
        sa.Column("saved_at", sa.Text(), nullable=False),
        sa.Column("feedback_id", sa.Text()),
    )


def downgrade():
    for table in ("bookmarks", "feedback", "session_skills", "sessions", "profile"):
        op.drop_table(table)
