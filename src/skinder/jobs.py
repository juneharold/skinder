"""Durable jobs. Every user mutation locks the user before its job or session."""
import hashlib
import json
from datetime import UTC, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from .database import JOB_ADMISSION_LOCK, Database
from .matcher import PROMPT_VERSION
from .models import MatchInput, Profile, Recommendation, Skill
from .orm import (
    CatalogSkillRecord,
    DailyUsageRecord,
    FeedbackRecord,
    MatchJobRecord,
    MatchSessionRecord,
    MatchSessionSkillRecord,
    ProfileRecord,
    ProviderAttemptRecord,
    UserDailyUsageRecord,
    UserRecord,
    WorkerRecord,
    new_id,
)
from .provider import Pricing, ProviderOutcome
from .settings import Settings


class JobError(Exception):
    def __init__(self, status_code: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status_code, self.detail, self.code = status_code, detail, code


class JobStore:
    def __init__(self, db: Database, settings: Settings, users):
        self.db, self.settings, self.users = db, settings, users

    @staticmethod
    def _now(session):
        return session.scalar(select(func.clock_timestamp()))

    @staticmethod
    def _user(session, user_id):
        user = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
        if user is None:
            raise JobError(404, "Account not found.")
        return user

    @classmethod
    def _job(cls, session, job_id, user_id=None, *, skip_locked=False):
        try:
            UUID(job_id)
        except (ValueError, TypeError, AttributeError):
            raise JobError(404, "Match job not found.") from None
        owner = session.scalar(select(MatchJobRecord.user_id).where(MatchJobRecord.id == job_id))
        if owner is None or (user_id is not None and owner != user_id):
            raise JobError(404, "Match job not found.")
        cls._user(session, owner)
        return session.scalar(select(MatchJobRecord).where(MatchJobRecord.id == job_id).with_for_update(skip_locked=skip_locked))

    def submit(self, user_id: str, value: MatchInput, idempotency_key: str):
        digest = hashlib.sha256(json.dumps(value.model_dump(), sort_keys=True).encode()).hexdigest()
        with self.db.db_sessions.begin() as session:
            self._user(session, user_id)
            previous = session.scalar(select(MatchJobRecord).where(
                MatchJobRecord.user_id == user_id, MatchJobRecord.idempotency_key == idempotency_key))
            if previous:
                if previous.request_hash != digest:
                    raise JobError(409, "This request key was already used for different input.", "idempotency_conflict")
                row = session.get(MatchSessionRecord, previous.session_id)
                # Return the original job even when this task now has a newer continuation.
                result = self.users.hydrate_session(session, user_id, row)
                from .models import Job
                return result.model_copy(update={"status": previous.status, "latest_job": Job(
                    id=previous.id, status=previous.status, model=previous.model,
                    error_code=previous.error_code, error=previous.error), "error": previous.error,
                    "recommendations": [Recommendation.model_validate(item) for item in (
                        previous.result if previous.status == "completed"
                        else previous.input.get("previous_recommendations", []))]})
            active = session.scalar(select(MatchJobRecord.id).where(MatchJobRecord.user_id == user_id,
                MatchJobRecord.status.in_(["queued", "running"])))
            if active:
                raise JobError(409, "A match is already active. Wait for it or cancel it first.", "active_job")
            now = self._now(session)
            day = now.astimezone(UTC).date()
            usage = session.get(UserDailyUsageRecord, (user_id, day))
            if usage is None:
                usage = UserDailyUsageRecord(user_id=user_id, day=day, accepted_jobs=0)
                session.add(usage)
            if usage.accepted_jobs >= self.settings.matches_per_user_per_day:
                raise JobError(429, "Your daily matching limit has been reached.", "daily_limit")
            if value.session_id:
                try:
                    UUID(value.session_id)
                except (ValueError, TypeError, AttributeError):
                    raise JobError(404, "Task session not found.") from None
                row = session.scalar(select(MatchSessionRecord).where(
                    MatchSessionRecord.user_id == user_id, MatchSessionRecord.id == value.session_id))
                if row is None:
                    raise JobError(404, "Task session not found.")
                if (row.task, row.agent) != (value.task, value.agent):
                    raise JobError(409, "A continuation must use the same task and target agent.")
            else:
                row = MatchSessionRecord(id=new_id(), user_id=user_id, task=value.task,
                    agent=value.agent, recommendations=[])
                session.add(row)
                session.flush()
            saved_profile = session.get(ProfileRecord, user_id)
            profile = Profile.model_validate(saved_profile.data if saved_profile else {}).model_dump()
            profile["agent"] = value.agent
            feedback = [{"skill_id": event.skill_id, "action": event.action, "reason": event.reason,
                         "task": task, "agent": agent}
                for event, task, agent in session.execute(select(FeedbackRecord, MatchSessionRecord.task,
                    MatchSessionRecord.agent).join(MatchSessionRecord,
                    MatchSessionRecord.id == FeedbackRecord.session_id).where(
                    FeedbackRecord.user_id == user_id, MatchSessionRecord.agent == value.agent,
                    (FeedbackRecord.action == "save") | FeedbackRecord.reason.in_(["wrong-style", "too-much-setup"]))
                    .order_by(FeedbackRecord.mutation_seq.desc()).limit(30))]
            from .sources import match_metadata
            for event in feedback:
                skill = session.get(CatalogSkillRecord, event["skill_id"])
                if skill:
                    event["skill"] = match_metadata(Skill.model_validate(skill.data))
            seen = list(session.scalars(select(FeedbackRecord.skill_id).where(
                FeedbackRecord.user_id == user_id, FeedbackRecord.session_id == row.id)))
            job = MatchJobRecord(id=new_id(), user_id=user_id, session_id=row.id,
                idempotency_key=idempotency_key, request_hash=digest, status="queued",
                input={"task": value.task, "agent": value.agent, "profile": profile,
                       "feedback": feedback, "seen_ids": seen,
                       "previous_recommendations": row.recommendations}, model=self.settings.letsur_model,
                prompt_version=PROMPT_VERSION)
            session.add(job)
            session.flush()
            row.latest_job_id, row.updated_at = job.id, now
            usage.accepted_jobs += 1
            session.flush()
            return self.users.hydrate_session(session, user_id, row)

    def cancel(self, user_id: str, job_id: str):
        with self.db.db_sessions.begin() as session:
            job = self._job(session, job_id, user_id)
            if job.status in {"queued", "running"}:
                job.status, job.finished_at = "cancelled", self._now(session)
                job.lease_token += 1
                job.lease_expires_at = None
            row = session.get(MatchSessionRecord, job.session_id)
            session.flush()
            return self.users.hydrate_session(session, user_id, row)

    def _valid(self, job, token, now):
        return (job.status == "running" and job.lease_token == token and
                job.lease_expires_at > now and job.deadline_at > now)

    @staticmethod
    def _terminal(job, now, code):
        job.status, job.finished_at, job.error_code = "failed", now, code
        job.error = "Matching could not be completed. You can start a new match."
        job.lease_token += 1
        job.lease_expires_at = None

    def reap(self):
        """Recover expired work even while the model or its pricing is unavailable."""
        with self.db.db_sessions.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": JOB_ADMISSION_LOCK})
            self._reap(session)

    def _reap(self, session):
        now = self._now(session)
        expired = list(session.scalars(select(MatchJobRecord.id).where(
            ((MatchJobRecord.status == "running") & ((MatchJobRecord.lease_expires_at <= now) |
                (MatchJobRecord.deadline_at <= now))) |
            ((MatchJobRecord.status == "queued") &
                (MatchJobRecord.created_at <= now - timedelta(seconds=self.settings.queue_timeout_seconds))))))
        for job_id in expired:
            job = self._job(session, job_id)
            now = self._now(session)
            if job.status not in {"queued", "running"}:
                continue
            if job.status == "running" and job.lease_expires_at > now and job.deadline_at > now:
                continue
            if job.status == "queued" and job.created_at > now - timedelta(seconds=self.settings.queue_timeout_seconds):
                continue
            if job.dispatch_started:
                self._terminal(job, now, "provider_outcome_unknown")
            elif (job.status == "queued" or job.claim_count >= self.settings.max_job_claims or
                  (job.deadline_at and job.deadline_at <= now)):
                self._terminal(job, now, "job_expired")
            else:
                job.status, job.worker_id, job.lease_expires_at = "queued", None, None
                job.lease_token += 1
        session.flush()

    def claim(self, worker_id: str):
        with self.db.db_sessions.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": JOB_ADMISSION_LOCK})
            self._reap(session)
            live = session.scalar(select(func.count()).select_from(MatchJobRecord).where(
                MatchJobRecord.status == "running"))
            if live >= self.settings.max_concurrent_jobs:
                return None
            job_id = session.scalar(select(MatchJobRecord.id).where(MatchJobRecord.status == "queued")
                .order_by(MatchJobRecord.created_at, MatchJobRecord.id).limit(1))
            if job_id is None:
                return None
            job = self._job(session, job_id, skip_locked=True)
            now = self._now(session)
            if job is None or job.status != "queued":
                return None
            job.status, job.worker_id = "running", worker_id
            job.lease_token += 1
            job.claim_count += 1
            job.started_at = job.started_at or now
            job.deadline_at = job.deadline_at or now + timedelta(seconds=self.settings.job_timeout_seconds)
            job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            session.flush()
            return job

    def heartbeat(self, job_id, token):
        with self.db.db_sessions.begin() as session:
            job = self._job(session, job_id)
            now = self._now(session)
            if not self._valid(job, token, now):
                return False
            job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            return True

    def dispatch(self, job_id, token, request: dict, pricing: Pricing):
        with self.db.db_sessions.begin() as session:
            job = self._job(session, job_id)
            now = self._now(session)
            if not self._valid(job, token, now) or job.dispatch_started:
                return False
            day = now.astimezone(UTC).date()
            session.execute(insert(DailyUsageRecord).values(day=day, spent=0, reserved=0).on_conflict_do_nothing())
            usage = session.scalar(select(DailyUsageRecord).where(DailyUsageRecord.day == day).with_for_update())
            now = self._now(session)
            if not self._valid(job, token, now):
                return False
            reserve = pricing.reserve(request)
            if usage.spent + usage.reserved + reserve > self.settings.daily_budget_units:
                self._terminal(job, now, "daily_budget_exhausted")
                return False
            usage.reserved += reserve
            job.dispatch_started, job.provider_request = True, request
            job.budget_date, job.reserved_units = day, reserve
            session.add(ProviderAttemptRecord(job_id=job.id, lease_token=token,
                                             model=job.model, pricing=pricing.as_dict()))
            return True

    def finish(self, job_id, token, outcome: ProviderOutcome, timings=None):
        with self.db.db_sessions.begin() as session:
            job = self._job(session, job_id)
            now = self._now(session)
            attempt = session.scalar(select(ProviderAttemptRecord).where(ProviderAttemptRecord.job_id == job_id))
            # Usage is safe to reconcile after cancellation or lease expiry; it never publishes output.
            if attempt and attempt.lease_token == token and attempt.finished_at is None:
                attempt.finished_at, attempt.status = now, "failed" if outcome.error_code else "completed"
                attempt.request_id, attempt.error_type, attempt.error_ref = outcome.request_id, outcome.error_code, outcome.error_ref
                attempt.input_tokens, attempt.output_tokens = outcome.input_tokens, outcome.output_tokens
                attempt.cost, attempt.currency = outcome.cost, outcome.currency
                if outcome.cost is not None and outcome.currency == "unit" and not job.reservation_reconciled:
                    usage = session.scalar(select(DailyUsageRecord).where(DailyUsageRecord.day == job.budget_date).with_for_update())
                    usage.reserved -= job.reserved_units
                    usage.spent += outcome.cost
                    job.reservation_reconciled = True
            now = self._now(session)
            if not self._valid(job, token, now):
                return False
            job.stage_timings = timings or {}
            if outcome.error_code:
                self._terminal(job, now, outcome.error_code)
                return True
            if outcome.result is None:
                raise ValueError("A successful outcome must contain recommendations")
            result = outcome.result.model_dump()["recommendations"]
            job.status, job.result, job.finished_at = "completed", result, now
            job.lease_expires_at = None
            row = session.get(MatchSessionRecord, job.session_id)
            row.recommendations, row.displayed_job_id, row.updated_at = result, job.id, now
            for recommendation in result:
                session.execute(insert(MatchSessionSkillRecord).values(user_id=job.user_id,
                    session_id=job.session_id, skill_id=recommendation["skill_id"]).on_conflict_do_nothing())
            return True

    def remaining(self, job_id, token):
        with self.db.db_sessions() as session:
            job = session.get(MatchJobRecord, job_id)
            now = self._now(session)
            if job is None or not self._valid(job, token, now):
                return 0
            return (job.deadline_at - now).total_seconds()

    def worker_heartbeat(self, worker_id, ready=True):
        with self.db.db_sessions.begin() as session:
            now = self._now(session)
            session.execute(insert(WorkerRecord).values(id=worker_id, last_seen=now,
                model=self.settings.letsur_model, ready=ready).on_conflict_do_update(
                    index_elements=[WorkerRecord.id], set_={"last_seen": now, "ready": ready,
                    "model": self.settings.letsur_model}))

    def matching_available(self):
        with self.db.db_sessions() as session:
            cutoff = self._now(session) - timedelta(seconds=self.settings.job_lease_seconds)
            return bool(session.scalar(select(WorkerRecord.id).where(WorkerRecord.ready.is_(True),
                WorkerRecord.model == self.settings.letsur_model, WorkerRecord.last_seen > cutoff).limit(1)))
