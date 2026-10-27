from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .database import Database
from .models import Agent, Feedback, FeedbackInput, Job, MatchSession, Profile
from .orm import (
    BookmarkRecord,
    FeedbackRecord,
    MatchJobRecord,
    MatchSessionRecord,
    MatchSessionSkillRecord,
    ProfileRecord,
    UserRecord,
)


class Bookmark(TypedDict):
    skill_id: str
    saved_at: str
    feedback_id: str | None


class PreferenceEvent(TypedDict):
    skill_id: str
    action: Literal["save", "pass"]
    reason: Literal["wrong-task", "wrong-style", "too-much-setup"] | None
    task: str
    agent: Agent


def _valid_id(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _feedback(row: FeedbackRecord) -> Feedback:
    return Feedback(id=row.id, session_id=row.session_id, skill_id=row.skill_id,
        action=row.action, reason=row.reason, created_at=row.created_at.isoformat())


class UserStore:
    def __init__(self, database: Database) -> None:
        self.engine = database.engine
        self.db_sessions = database.db_sessions

    @contextmanager
    def _write_session(self, user_id: str) -> Iterator[tuple[Session, UserRecord]]:
        with self.db_sessions.begin() as db:
            user = db.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
            if user is None:
                raise ValueError("User does not exist.")
            yield db, user

    @contextmanager
    def _snapshot(self) -> Iterator[Session]:
        with self.db_sessions() as db:
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            yield db

    def get_profile(self, user_id: str) -> Profile:
        with self.db_sessions() as db:
            row = db.get(ProfileRecord, user_id)
            return Profile.model_validate(row.data) if row else Profile()

    def set_profile(self, user_id: str, profile: Profile) -> Profile:
        with self._write_session(user_id) as (db, user):
            user.mutation_seq += 1
            db.merge(ProfileRecord(user_id=user_id, data=profile.model_dump()))
        return profile

    @staticmethod
    def hydrate_session(db: Session, user_id: str, row: MatchSessionRecord) -> MatchSession:
        if row.user_id != user_id:
            raise ValueError("Task session does not belong to this user.")
        job = db.scalar(select(MatchJobRecord).where(
            MatchJobRecord.user_id == user_id, MatchJobRecord.id == row.latest_job_id))
        feedback = db.scalars(select(FeedbackRecord).where(
            FeedbackRecord.user_id == user_id, FeedbackRecord.session_id == row.id,
        ).order_by(FeedbackRecord.mutation_seq))
        return MatchSession(id=row.id, task=row.task, agent=row.agent, status=job.status,
            latest_job=Job.model_validate(job, from_attributes=True),
            recommendations=row.recommendations, feedback=[_feedback(item) for item in feedback],
            error=job.error, created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())

    def get_match_session(self, user_id: str, session_id: str) -> MatchSession | None:
        if not _valid_id(session_id):
            return None
        with self._snapshot() as db:
            row = db.scalar(select(MatchSessionRecord).where(
                MatchSessionRecord.user_id == user_id, MatchSessionRecord.id == session_id))
            return self.hydrate_session(db, user_id, row) if row else None

    def get_current_match_session(self, user_id: str) -> MatchSession | None:
        with self._snapshot() as db:
            row = db.scalar(select(MatchSessionRecord).where(MatchSessionRecord.user_id == user_id)
                .order_by(MatchSessionRecord.updated_at.desc(), MatchSessionRecord.id).limit(1))
            return self.hydrate_session(db, user_id, row) if row else None

    def get_seen_skill_ids(self, user_id: str, session_id: str) -> set[str]:
        if not _valid_id(session_id):
            return set()
        with self.db_sessions() as db:
            return set(db.scalars(select(FeedbackRecord.skill_id).where(
                FeedbackRecord.user_id == user_id, FeedbackRecord.session_id == session_id)))

    def add_feedback(self, user_id: str, value: FeedbackInput) -> Feedback:
        if not _valid_id(value.session_id):
            raise ValueError("Feedback requires a skill recommended in this task session.")
        with self._write_session(user_id) as (db, user):
            if db.get(MatchSessionSkillRecord, (user_id, value.session_id, value.skill_id)) is None:
                raise ValueError("Feedback requires a skill recommended in this task session.")
            previous = db.scalar(select(FeedbackRecord).where(
                FeedbackRecord.user_id == user_id, FeedbackRecord.session_id == value.session_id,
                FeedbackRecord.skill_id == value.skill_id))
            # Retries never recreate removed bookmarks, even during a continuation.
            if previous and previous.action == value.action and previous.reason == value.reason:
                return _feedback(previous)
            if previous:
                raise ValueError("Undo the existing feedback before changing it.")
            self._require_feedback_idle(db, user_id, value.session_id)
            user.mutation_seq += 1
            row = FeedbackRecord(user_id=user_id, mutation_seq=user.mutation_seq, **value.model_dump())
            db.add(row)
            db.flush()
            if value.action == "save" and db.get(BookmarkRecord, (user_id, value.skill_id)) is None:
                db.add(BookmarkRecord(user_id=user_id, skill_id=value.skill_id,
                    created_seq=user.mutation_seq, feedback_id=row.id))
            return _feedback(row)

    @staticmethod
    def _require_feedback_idle(db: Session, user_id: str, session_id: str) -> None:
        active = db.scalar(select(MatchJobRecord.id).where(
            MatchJobRecord.user_id == user_id, MatchJobRecord.session_id == session_id,
            MatchJobRecord.status.in_(("queued", "running"))).limit(1))
        if active:
            raise ValueError("Wait for matching to finish or cancel it before changing feedback.")

    def undo_feedback(self, user_id: str, feedback_id: str) -> None:
        if not _valid_id(feedback_id):
            return
        with self._write_session(user_id) as (db, user):
            row = db.scalar(select(FeedbackRecord).where(
                FeedbackRecord.user_id == user_id, FeedbackRecord.id == feedback_id))
            if row is None:
                return
            self._require_feedback_idle(db, user_id, row.session_id)
            user.mutation_seq += 1
            db.delete(row)
            bookmark = db.scalar(select(BookmarkRecord).where(
                BookmarkRecord.user_id == user_id, BookmarkRecord.feedback_id == feedback_id))
            if bookmark is None:
                return
            other = db.scalar(select(FeedbackRecord.id).where(
                FeedbackRecord.user_id == user_id, FeedbackRecord.skill_id == row.skill_id,
                FeedbackRecord.action == "save", FeedbackRecord.mutation_seq >= bookmark.created_seq,
            ).order_by(FeedbackRecord.mutation_seq.desc()).limit(1))
            if other:
                bookmark.feedback_id = other
                return
            db.delete(bookmark)

    def get_bookmarks(self, user_id: str) -> list[Bookmark]:
        with self.db_sessions() as db:
            rows = db.scalars(select(BookmarkRecord).where(BookmarkRecord.user_id == user_id)
                .order_by(BookmarkRecord.created_seq.desc()))
            return [{"skill_id": row.skill_id, "saved_at": row.saved_at.isoformat(),
                     "feedback_id": row.feedback_id} for row in rows]

    def add_bookmark(self, user_id: str, skill_id: str) -> None:
        with self._write_session(user_id) as (db, user):
            user.mutation_seq += 1
            bookmark = db.get(BookmarkRecord, (user_id, skill_id))
            if bookmark:
                bookmark.feedback_id = None
                return
            db.add(BookmarkRecord(user_id=user_id, skill_id=skill_id,
                created_seq=user.mutation_seq))

    def remove_bookmark(self, user_id: str, skill_id: str) -> None:
        with self._write_session(user_id) as (db, user):
            user.mutation_seq += 1
            db.execute(delete(BookmarkRecord).where(
                BookmarkRecord.user_id == user_id, BookmarkRecord.skill_id == skill_id))

    def get_preference_events(self, user_id: str, agent: Agent) -> list[PreferenceEvent]:
        with self.db_sessions() as db:
            rows = db.execute(select(
                FeedbackRecord.skill_id, FeedbackRecord.action, FeedbackRecord.reason,
                MatchSessionRecord.task, MatchSessionRecord.agent,
            ).join(MatchSessionRecord, MatchSessionRecord.id == FeedbackRecord.session_id).where(
                FeedbackRecord.user_id == user_id, MatchSessionRecord.user_id == user_id,
                MatchSessionRecord.agent == agent,
                or_(FeedbackRecord.action == "save",
                    FeedbackRecord.reason.in_(("wrong-style", "too-much-setup"))),
            ).order_by(FeedbackRecord.mutation_seq.desc()).limit(30)).mappings()
            return [dict(row) for row in rows]
