"""PostgreSQL records and constraints for accounts, durable work, and search."""
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Computed, Date, DateTime, Float,
    ForeignKey, ForeignKeyConstraint, Identity, Index, Integer, Numeric, Text,
    UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_id)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    mutation_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class LoginSessionRecord(Base):
    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # hash of the opaque cookie
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProfileRecord(Base):
    __tablename__ = "profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class MatchSessionRecord(Base):
    __tablename__ = "match_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        ForeignKeyConstraint(["user_id", "latest_job_id"], ["match_jobs.user_id", "match_jobs.id"],
            name="session_latest_job", use_alter=True, deferrable=True, initially="DEFERRED"),
        ForeignKeyConstraint(["user_id", "displayed_job_id"], ["match_jobs.user_id", "match_jobs.id"],
            name="session_displayed_job", use_alter=True, deferrable=True, initially="DEFERRED"),
        Index("sessions_user_updated", "user_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text)
    latest_job_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    displayed_job_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class MatchJobRecord(Base):
    __tablename__ = "match_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "id"), UniqueConstraint("user_id", "idempotency_key"),
        ForeignKeyConstraint(["user_id", "session_id"], ["match_sessions.user_id", "match_sessions.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('queued','running','completed','failed','cancelled')"),
        Index("one_active_job_per_user", "user_id", unique=True, postgresql_where=text("status IN ('queued','running')")),
        Index("jobs_queue", "created_at", "id", postgresql_where=text("status='queued'")),
        Index("jobs_leases", "lease_expires_at", postgresql_where=text("status='running'")),
    )
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued")
    input: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    provider_request: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[list | None] = mapped_column(JSONB)
    stage_timings: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[int] = mapped_column(BigInteger, default=0)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    dispatch_started: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_date: Mapped[date | None] = mapped_column(Date)
    reserved_units: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    reservation_reconciled: Mapped[bool] = mapped_column(Boolean, default=False)


class ProviderAttemptRecord(Base):
    __tablename__ = "provider_attempts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("match_jobs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(Text, default="dispatched")
    model: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_ref: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(Text)
    pricing: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyUsageRecord(Base):
    __tablename__ = "daily_usage"
    __table_args__ = (CheckConstraint("spent >= 0 AND reserved >= 0"),)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    spent: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    reserved: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))


class UserDailyUsageRecord(Base):
    __tablename__ = "user_daily_usage"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    accepted_jobs: Mapped[int] = mapped_column(Integer, default=0)


class WorkerRecord(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(Text)
    ready: Mapped[bool] = mapped_column(Boolean, default=True)


class MatchSessionSkillRecord(Base):
    __tablename__ = "match_session_skills"
    __table_args__ = (ForeignKeyConstraint(["user_id", "session_id"],
        ["match_sessions.user_id", "match_sessions.id"], ondelete="CASCADE"),)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    session_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)


class FeedbackRecord(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "id"), UniqueConstraint("user_id", "session_id", "skill_id"),
        ForeignKeyConstraint(["user_id", "session_id", "skill_id"],
            ["match_session_skills.user_id", "match_session_skills.session_id", "match_session_skills.skill_id"]),
    )
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    session_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    skill_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    mutation_seq: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class BookmarkRecord(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (ForeignKeyConstraint(["user_id", "feedback_id"],
        ["feedback.user_id", "feedback.id"], deferrable=True, initially="DEFERRED"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    created_seq: Mapped[int] = mapped_column(BigInteger)
    feedback_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))


class CatalogSkillRecord(Base):
    __tablename__ = "catalog_skills"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(Text, default="")
    compatibility: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    retrieval_text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)
    search_vector: Mapped[object] = mapped_column(TSVECTOR, Computed(
        "setweight(to_tsvector('simple', coalesce(name,'')), 'A') || "
        "setweight(to_tsvector('simple', coalesce(tags,'') || ' ' || coalesce(author,'')), 'B') || "
        "setweight(to_tsvector('simple', coalesce(description,'')), 'C')", persisted=True))
    __table_args__ = (Index("catalog_search", "search_vector", postgresql_using="gin"),
        Index("catalog_name", func.lower(name), id))


class CatalogAliasRecord(Base):
    __tablename__ = "catalog_aliases"
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


class SourceCacheRecord(Base):
    __tablename__ = "source_cache"
    skill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[float] = mapped_column(Float)
    content: Mapped[str] = mapped_column(Text)


class SyncRunRecord(Base):
    __tablename__ = "sync_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)


class SyncPartitionRecord(Base):
    __tablename__ = "sync_partitions"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    status: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB)


class SkillEmbeddingRecord(Base):
    __tablename__ = "skill_embeddings"
    __table_args__ = (
        UniqueConstraint("skill_id", "text_hash", "model_fingerprint"),
        Index("embeddings_cosine", "vector", postgresql_using="hnsw", postgresql_ops={"vector": "vector_cosine_ops"}),
    )
    vector_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    skill_id: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    model_fingerprint: Mapped[str] = mapped_column(Text)
    vector: Mapped[object] = mapped_column(Vector(384))
