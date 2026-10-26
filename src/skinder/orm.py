"""Database records; JSON payloads and timestamps retain their existing storage format."""

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Personal data and the replaceable catalog have separate databases and migration histories.
class UserBase(DeclarativeBase):
    pass


class CatalogBase(DeclarativeBase):
    pass


class ProfileRecord(UserBase):
    __tablename__ = "profile"
    __table_args__ = (CheckConstraint("id=1"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class MatchSessionRecord(UserBase):
    __tablename__ = "match_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(Text, server_default=text("'codex'"))
    status: Mapped[str] = mapped_column(Text)
    recommendations: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


# This cumulative history authorizes feedback even after a newer batch replaces the displayed one.
class MatchSessionSkillRecord(UserBase):
    __tablename__ = "match_session_skills"

    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("match_sessions.id"), primary_key=True,
    )
    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)


class FeedbackRecord(UserBase):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("session_id", "skill_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str | None] = mapped_column(Text, ForeignKey("match_sessions.id"))
    skill_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


# Skill IDs cross the database boundary, so user records cannot reference catalog rows as foreign keys.
class BookmarkRecord(UserBase):
    __tablename__ = "bookmarks"

    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    saved_at: Mapped[str] = mapped_column(Text)
    # None denotes an independent save; otherwise undo may transfer or remove the bookmark.
    feedback_id: Mapped[str | None] = mapped_column(Text)


class CatalogSkillRecord(CatalogBase):
    __tablename__ = "catalog_skills"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Preserve the full API payload while separate columns support filtering and retrieval.
    data: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    compatibility: Mapped[str | None] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("catalog_category", category),
        Index("catalog_name", name.collate("NOCASE"), id),
    )


class CatalogAliasRecord(CatalogBase):
    __tablename__ = "catalog_aliases"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(Text)


class SourceCacheRecord(CatalogBase):
    __tablename__ = "source_cache"

    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[float] = mapped_column(REAL)
    content: Mapped[str] = mapped_column(Text)


class SyncRunRecord(CatalogBase):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class SyncPartitionRecord(CatalogBase):
    __tablename__ = "sync_partitions"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    data: Mapped[str] = mapped_column(Text)


class RetrievalMetaRecord(CatalogBase):
    __tablename__ = "retrieval_meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SkillEmbeddingRecord(CatalogBase):
    __tablename__ = "skill_embeddings"
    __table_args__ = (
        UniqueConstraint("skill_id", "text_hash", "model_fingerprint"),
        # Published search indexes retain vector IDs; deleted IDs must never identify new vectors.
        {"sqlite_autoincrement": True},
    )

    vector_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    model_fingerprint: Mapped[str] = mapped_column(Text)
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class RetrievalMemberRecord(CatalogBase):
    __tablename__ = "retrieval_members"
    __table_args__ = ({"sqlite_with_rowid": False},)

    generation: Mapped[str] = mapped_column(Text, primary_key=True)
    vector_id: Mapped[int] = mapped_column(Integer, primary_key=True)
