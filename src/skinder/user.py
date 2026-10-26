import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from .database import create_database
from .models import Agent, Engine, Feedback, FeedbackInput, MatchSession, Profile, Recommendation
from .orm import (
    BookmarkRecord,
    FeedbackRecord,
    MatchSessionRecord,
    MatchSessionSkillRecord,
    ProfileRecord,
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


def now() -> str:
    return datetime.now(UTC).isoformat()


class UserStore:
    def __init__(self, path: str | Path) -> None:
        self.engine = create_database(path, "user")
        self.db_sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with self.db_sessions.begin() as db:
            # Reserve the writer before reading so concurrent operations cannot
            # make the same decision and then fail while upgrading a read lock.
            db.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            yield db

    def recover_interrupted_sessions(self) -> None:
        """Recover after startup has established exclusive ownership of this user database."""
        with self._write_session() as db:
            db.execute(
                update(MatchSessionRecord)
                .where(MatchSessionRecord.status.in_(("running", "pending")))
                .values(
                    status="failed",
                    error="Matching was interrupted by a server restart. Try again when ready.",
                    updated_at=now(),
                )
            )

    def close(self) -> None:
        self.engine.dispose()

    def get_profile(self) -> Profile:
        with self.db_sessions() as db:
            row = db.get(ProfileRecord, 1)
            return Profile.model_validate_json(row.data) if row else Profile()

    def set_profile(self, profile: Profile) -> Profile:
        with self._write_session() as db:
            db.merge(ProfileRecord(id=1, data=profile.model_dump_json()))
        return profile

    def get_match_session(self, session_id: str) -> MatchSession | None:
        with self.db_sessions() as db:
            row = db.get(MatchSessionRecord, session_id)
            if row is None:
                return None
            feedback = db.scalars(
                select(FeedbackRecord)
                .where(FeedbackRecord.session_id == session_id)
                .order_by(FeedbackRecord.created_at)
            )
            return MatchSession(
                id=row.id, task=row.task, agent=row.agent, engine=row.engine, status=row.status,
                recommendations=json.loads(row.recommendations),
                feedback=[Feedback.model_validate(item, from_attributes=True) for item in feedback],
                error=row.error, created_at=row.created_at, updated_at=row.updated_at,
            )

    def get_current_match_session(self) -> MatchSession | None:
        with self.db_sessions() as db:
            session_id = db.scalar(
                select(MatchSessionRecord.id).order_by(MatchSessionRecord.updated_at.desc()).limit(1)
            )
        return self.get_match_session(session_id) if session_id else None

    def start_match_session(
        self, task: str, agent: Agent, session_id: str | None = None,
        *, engine: Engine = "codex",
    ) -> MatchSession | None:
        timestamp = now()
        with self._write_session() as db:
            if session_id:
                # Keep the previous batch available until a continuation successfully replaces it.
                db.execute(
                    update(MatchSessionRecord)
                    .where(MatchSessionRecord.id == session_id)
                    .values(status="running", error=None, updated_at=timestamp)
                )
            else:
                session_id = str(uuid4())
                db.add(MatchSessionRecord(
                    id=session_id, task=task, agent=agent, engine=engine, status="running",
                    recommendations="[]", created_at=timestamp, updated_at=timestamp,
                ))
        return self.get_match_session(session_id)

    def finish_match_session(
        self, session_id: str, status: Literal["completed", "failed", "cancelled"],
        recommendations: Sequence[Recommendation] | None = None, error: str | None = None,
    ) -> None:
        values = {"status": status, "error": error, "updated_at": now()}
        # None preserves the last batch on failure/cancellation; [] records a successful no-fit.
        if recommendations is not None:
            values["recommendations"] = json.dumps([r.model_dump() for r in recommendations])
        with self._write_session() as db:
            # Cancellation is authoritative even if a subprocess produces a late result.
            changed = db.execute(
                update(MatchSessionRecord)
                .where(MatchSessionRecord.id == session_id, MatchSessionRecord.status == "running")
                .values(**values)
            )
            if not changed.rowcount or recommendations is None:
                return
            # Feedback remains valid for recommendations from any earlier batch in this session.
            known = set(db.scalars(
                select(MatchSessionSkillRecord.skill_id).where(
                    MatchSessionSkillRecord.session_id == session_id
                )
            ))
            db.add_all([
                MatchSessionSkillRecord(session_id=session_id, skill_id=skill_id)
                for skill_id in {r.skill_id for r in recommendations} - known
            ])

    def get_seen_skill_ids(self, session_id: str) -> set[str]:
        # Only reviewed skills are excluded; an unreviewed recommendation can appear again.
        with self.db_sessions() as db:
            return set(db.scalars(
                select(FeedbackRecord.skill_id).where(FeedbackRecord.session_id == session_id)
            ))

    def add_feedback(self, value: FeedbackInput) -> Feedback:
        with self._write_session() as db:
            if db.get(MatchSessionSkillRecord, (value.session_id, value.skill_id)) is None:
                raise ValueError("Feedback requires a skill recommended in this task session.")
            previous = db.scalar(select(FeedbackRecord).where(
                FeedbackRecord.session_id == value.session_id,
                FeedbackRecord.skill_id == value.skill_id,
            ))
            # Identical retries are allowed during matching and never recreate a removed bookmark.
            if previous and previous.action == value.action and previous.reason == value.reason:
                return Feedback.model_validate(previous, from_attributes=True)
            if previous:
                raise ValueError("Undo the existing feedback before changing it.")
            self._require_feedback_idle(db.get(MatchSessionRecord, value.session_id))
            result = Feedback(**value.model_dump(), id=str(uuid4()), created_at=now())
            db.add(FeedbackRecord(**result.model_dump()))
            if value.action == "save" and db.get(BookmarkRecord, value.skill_id) is None:
                db.add(BookmarkRecord(
                    skill_id=value.skill_id, saved_at=result.created_at, feedback_id=result.id,
                ))
        return result

    @staticmethod
    def _require_feedback_idle(session: MatchSessionRecord) -> None:
        if session.status in ("pending", "running"):
            raise ValueError("Wait for matching to finish or cancel it before changing feedback.")

    def undo_feedback(self, feedback_id: str) -> None:
        with self._write_session() as db:
            row = db.get(FeedbackRecord, feedback_id)
            if row is None:
                return
            self._require_feedback_idle(db.get(MatchSessionRecord, row.session_id))
            db.delete(row)
            # The following queries flush this deletion before looking for another save owner.
            bookmark = db.scalar(
                select(BookmarkRecord).where(BookmarkRecord.feedback_id == feedback_id)
            )
            # Undo may only change a bookmark still owned by this feedback.
            if bookmark is None:
                return
            other = db.scalar(
                select(FeedbackRecord.id)
                .where(
                    FeedbackRecord.skill_id == row.skill_id,
                    FeedbackRecord.action == "save",
                    # Older saves must not restore an explicitly removed bookmark.
                    FeedbackRecord.created_at >= bookmark.saved_at,
                )
                .order_by(FeedbackRecord.created_at.desc()).limit(1)
            )
            if other:
                bookmark.feedback_id = other
            else:
                db.delete(bookmark)

    def get_bookmarks(self) -> list[Bookmark]:
        with self.db_sessions() as db:
            rows = db.scalars(select(BookmarkRecord).order_by(BookmarkRecord.saved_at.desc()))
            return [
                {"skill_id": row.skill_id, "saved_at": row.saved_at, "feedback_id": row.feedback_id}
                for row in rows
            ]

    def add_bookmark(self, skill_id: str) -> None:
        with self._write_session() as db:
            bookmark = db.get(BookmarkRecord, skill_id)
            if bookmark:
                # A direct save survives feedback undo and keeps its original timestamp.
                bookmark.feedback_id = None
            else:
                db.add(BookmarkRecord(skill_id=skill_id, saved_at=now()))

    def remove_bookmark(self, skill_id: str) -> None:
        # Review history still informs later matches even if the skill is no longer bookmarked.
        with self._write_session() as db:
            db.execute(delete(BookmarkRecord).where(BookmarkRecord.skill_id == skill_id))

    def get_preference_events(self, agent: Agent) -> list[PreferenceEvent]:
        # A plain pass and wrong-task reason are local exclusions, never global taste signals.
        with self.db_sessions() as db:
            rows = db.execute(
                select(
                    FeedbackRecord.skill_id, FeedbackRecord.action, FeedbackRecord.reason,
                    MatchSessionRecord.task, MatchSessionRecord.agent,
                )
                .join(MatchSessionRecord, MatchSessionRecord.id == FeedbackRecord.session_id)
                .where(
                    MatchSessionRecord.agent == agent,
                    or_(FeedbackRecord.action == "save",
                        FeedbackRecord.reason.in_(("wrong-style", "too-much-setup"))),
                )
                .order_by(FeedbackRecord.created_at.desc()).limit(30)
            ).mappings()
            return [dict(row) for row in rows]
