"""Cross-subsystem regressions found during integration review."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from skinder.jobs import JobStore
from skinder.models import MatchInput, MatchResult, Recommendation
from skinder.orm import DailyUsageRecord, MatchJobRecord, ProviderAttemptRecord
from skinder.provider import Pricing, ProviderOutcome
from skinder.sync import writer_lock
from skinder.user import UserStore


def store(db, settings):
    return JobStore(db, settings, UserStore(db))


def submit(jobs, user, key='first', session_id=None):
    return jobs.submit(user.id, MatchInput(task='Review Python code', agent='codex',
                       session_id=session_id), key)


def outcome(skill):
    return ProviderOutcome(result=MatchResult(recommendations=[Recommendation(
        skill_id=skill, task_fit='Useful for this task', taste_fit='Careful review', caveat=None)]),
        cost=Decimal('0.0002'), currency='unit')


def test_catalog_maintenance_does_not_block_job_admission(db, settings, users, catalog):
    jobs = store(db, settings)
    submit(jobs, users[0])
    with ThreadPoolExecutor(1) as pool, writer_lock(catalog):
        future = pool.submit(jobs.claim, 'worker')
        job = future.result(timeout=3)
        assert job.status == 'running' and job.user_id == users[0].id


def test_stale_predispatch_worker_cannot_reconcile_replacement_attempt(db, settings, users):
    jobs = store(db, settings)
    submit(jobs, users[0])
    old = jobs.claim('old-worker')
    with db.db_sessions.begin() as session:
        session.get(MatchJobRecord, old.id).lease_expires_at = (
            session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1))
    replacement = jobs.claim('replacement')
    assert jobs.dispatch(replacement.id, replacement.lease_token,
        {'messages': [], 'max_tokens': 10}, Pricing(Decimal('0.000001'), Decimal('0.000001')))
    assert not jobs.finish(old.id, old.lease_token, ProviderOutcome(error_code='matching_failed'))
    with db.db_sessions() as session:
        attempt = session.scalar(select(ProviderAttemptRecord))
        assert attempt.finished_at is None
        assert attempt.lease_token == replacement.lease_token
    assert jobs.finish(replacement.id, replacement.lease_token, outcome('correct'))
    with db.db_sessions() as session:
        attempt = session.scalar(select(ProviderAttemptRecord))
        assert attempt.status == 'completed' and attempt.cost == Decimal('0.0002')
        usage = session.scalar(select(DailyUsageRecord))
        assert usage.spent == Decimal('0.0002') and usage.reserved == 0


def test_old_idempotency_key_returns_that_jobs_batch(db, settings, users):
    jobs = store(db, settings)
    first = submit(jobs, users[0])
    job = jobs.claim('worker')
    jobs.finish(job.id, job.lease_token, outcome('first-skill'))
    second = submit(jobs, users[0], 'second', first.id)
    job = jobs.claim('worker')
    jobs.finish(job.id, job.lease_token, outcome('second-skill'))
    replay = submit(jobs, users[0])
    assert replay.latest_job.id == first.latest_job.id
    assert [item.skill_id for item in replay.recommendations] == ['first-skill']
    current = jobs.users.get_current_match_session(users[0].id)
    assert current.latest_job.id == second.latest_job.id
    assert [item.skill_id for item in current.recommendations] == ['second-skill']
