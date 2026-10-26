"""Remember the recommendation engine for each task."""

import sqlalchemy as sa
from alembic import op

revision = "0002_match_engine"
down_revision = "0001_user"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "match_sessions",
        sa.Column("engine", sa.Text(), nullable=False, server_default=sa.text("'codex'")),
    )


def downgrade():
    op.drop_column("match_sessions", "engine")
