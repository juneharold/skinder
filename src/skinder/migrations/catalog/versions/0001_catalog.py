"""Create the catalog, source cache, sync checkpoints, and retrieval metadata."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_skills",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("compatibility", sa.Text()),
        sa.Column("retrieval_text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("source_updated_at", sa.Text()),
    )
    op.create_index("catalog_category", "catalog_skills", ["category"])
    op.create_index("catalog_name", "catalog_skills", [sa.text("name COLLATE NOCASE"), "id"])
    op.create_table(
        "catalog_aliases",
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text()),
    )
    op.create_table(
        "source_cache",
        sa.Column("skill_id", sa.Text(), primary_key=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_updated_at", sa.Text()),
        sa.Column("fetched_at", sa.REAL(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_table(
        "sync_partitions",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_table(
        "retrieval_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "skill_embeddings",
        sa.Column("vector_id", sa.Integer(), primary_key=True),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("model_fingerprint", sa.Text(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("skill_id", "text_hash", "model_fingerprint"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "retrieval_members",
        sa.Column("generation", sa.Text(), primary_key=True),
        sa.Column("vector_id", sa.Integer(), primary_key=True),
        sqlite_with_rowid=False,
    )
    op.execute(
        "CREATE VIRTUAL TABLE skills_fts USING fts5("
        "skill_id UNINDEXED, name, description, tags, author, tokenize='unicode61')"
    )
    meta = sa.table("retrieval_meta", sa.column("key", sa.Text()), sa.column("value", sa.Text()))
    op.bulk_insert(meta, [
        {"key": "catalog_revision", "value": "0"},
        {"key": "identity", "value": str(uuid4())},
    ])
    for event in ("INSERT", "UPDATE OF text_hash", "DELETE"):
        trigger = event.split()[0].lower()
        op.execute(
            f"CREATE TRIGGER retrieval_catalog_{trigger} AFTER {event} ON catalog_skills "
            "BEGIN UPDATE retrieval_meta SET value=CAST(value AS INTEGER)+1 "
            "WHERE key='catalog_revision'; END"
        )


def downgrade():
    op.execute("DROP TABLE skills_fts")
    for table in (
        "retrieval_members", "skill_embeddings", "retrieval_meta", "sync_partitions",
        "sync_run", "source_cache", "catalog_aliases", "catalog_skills",
    ):
        op.drop_table(table)
