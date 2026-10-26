import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import sessionmaker

from .database import create_database
from .models import Feedback, FeedbackInput, MatchSession, Profile
from .orm import (
    BookmarkRecord,
    FeedbackRecord,
    MatchSessionRecord,
    ProfileRecord,
    SessionSkillRecord,
)


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path):
        self.engine = create_database(path, "state")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions.begin() as db:
            db.execute(
                update(MatchSessionRecord)
                .where(MatchSessionRecord.status.in_(("running", "pending")))
                .values(
                    status="failed",
                    error="Matching was interrupted by a server restart. Try again when ready.",
                    updated_at=now(),
                )
            )

    def close(self):
        self.engine.dispose()

    def profile(self):
        with self.sessions() as db:
            row = db.get(ProfileRecord, 1)
            return Profile.model_validate_json(row.data) if row else Profile()

    def set_profile(self, profile):
        with self.sessions.begin() as db:
            db.merge(ProfileRecord(id=1, data=profile.model_dump_json()))
        return profile

    def session(self, session_id):
        with self.sessions() as db:
            row = db.get(MatchSessionRecord, session_id)
            if row is None:
                return None
            feedback = db.scalars(
                select(FeedbackRecord)
                .where(FeedbackRecord.session_id == session_id)
                .order_by(FeedbackRecord.created_at)
            )
            return MatchSession(
                id=row.id, task=row.task, agent=row.agent, status=row.status,
                recommendations=json.loads(row.recommendations),
                feedback=[Feedback.model_validate(item, from_attributes=True) for item in feedback],
                error=row.error, created_at=row.created_at, updated_at=row.updated_at,
            )

    def current(self):
        with self.sessions() as db:
            session_id = db.scalar(
                select(MatchSessionRecord.id).order_by(MatchSessionRecord.updated_at.desc()).limit(1)
            )
        return self.session(session_id) if session_id else None

    def start(self, task, agent, session_id=None):
        timestamp = now()
        with self.sessions.begin() as db:
            if session_id:
                db.execute(
                    update(MatchSessionRecord)
                    .where(MatchSessionRecord.id == session_id)
                    .values(status="running", error=None, updated_at=timestamp)
                )
            else:
                session_id = str(uuid4())
                db.add(MatchSessionRecord(
                    id=session_id, task=task, agent=agent, status="running",
                    recommendations="[]", created_at=timestamp, updated_at=timestamp,
                ))
        return self.session(session_id)

    def finish(self, session_id, status, recommendations=None, error=None):
        values = {"status": status, "error": error, "updated_at": now()}
        if recommendations is not None:
            values["recommendations"] = json.dumps([r.model_dump() for r in recommendations])
        with self.sessions.begin() as db:
            # Cancellation is authoritative even if a subprocess produces a late result.
            changed = db.execute(
                update(MatchSessionRecord)
                .where(MatchSessionRecord.id == session_id, MatchSessionRecord.status == "running")
                .values(**values)
            )
            if not changed.rowcount or recommendations is None:
                return
            known = set(db.scalars(
                select(SessionSkillRecord.skill_id).where(
                    SessionSkillRecord.session_id == session_id
                )
            ))
            db.add_all([
                SessionSkillRecord(session_id=session_id, skill_id=skill_id)
                for skill_id in {r.skill_id for r in recommendations} - known
            ])

    def seen(self, session_id):
        with self.sessions() as db:
            return set(db.scalars(
                select(FeedbackRecord.skill_id).where(FeedbackRecord.session_id == session_id)
            ))

    def feedback(self, value: FeedbackInput):
        with self.sessions.begin() as db:
            if db.get(SessionSkillRecord, (value.session_id, value.skill_id)) is None:
                raise ValueError("Feedback requires a skill recommended in this task session.")
            previous = db.scalar(select(FeedbackRecord).where(
                FeedbackRecord.session_id == value.session_id,
                FeedbackRecord.skill_id == value.skill_id,
            ))
            if previous:
                if previous.action == value.action and previous.reason == value.reason:
                    return Feedback.model_validate(previous, from_attributes=True)
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
    def _require_feedback_idle(session):
        if session.status in ("pending", "running"):
            raise ValueError("Wait for matching to finish or cancel it before changing feedback.")

    def undo(self, feedback_id):
        with self.sessions.begin() as db:
            row = db.get(FeedbackRecord, feedback_id)
            if row is None:
                return
            self._require_feedback_idle(db.get(MatchSessionRecord, row.session_id))
            db.delete(row)
            bookmark = db.scalar(
                select(BookmarkRecord).where(BookmarkRecord.feedback_id == feedback_id)
            )
            if bookmark:
                other = db.scalar(
                    select(FeedbackRecord.id)
                    .where(
                        FeedbackRecord.skill_id == row.skill_id,
                        FeedbackRecord.action == "save",
                        FeedbackRecord.created_at >= bookmark.saved_at,
                    )
                    .order_by(FeedbackRecord.created_at.desc()).limit(1)
                )
                if other:
                    bookmark.feedback_id = other
                else:
                    db.delete(bookmark)

    def saved(self):
        with self.sessions() as db:
            rows = db.scalars(select(BookmarkRecord).order_by(BookmarkRecord.saved_at.desc()))
            return [
                {"skill_id": row.skill_id, "saved_at": row.saved_at, "feedback_id": row.feedback_id}
                for row in rows
            ]

    def save(self, skill_id):
        with self.sessions.begin() as db:
            bookmark = db.get(BookmarkRecord, skill_id)
            if bookmark:
                bookmark.feedback_id = None
            else:
                db.add(BookmarkRecord(skill_id=skill_id, saved_at=now()))

    def unsave(self, skill_id):
        with self.sessions.begin() as db:
            db.execute(delete(BookmarkRecord).where(BookmarkRecord.skill_id == skill_id))

    def preference_events(self, agent):
        # A plain pass and wrong-task reason are local exclusions, never global taste signals.
        with self.sessions() as db:
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
