"""Durability and concurrency contracts against isolated PostgreSQL schemas."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from skinder.jobs import JobError, JobStore
from skinder.models import FeedbackInput, MatchInput, MatchResult, Profile, Recommendation
from skinder.orm import (
    DailyUsageRecord,
    MatchJobRecord,
    MatchSessionSkillRecord,
    ProviderAttemptRecord,
    UserDailyUsageRecord,
    UserRecord,
)
from skinder.provider import Pricing, ProviderError, ProviderOutcome
from skinder.user import UserStore
from skinder.worker import Worker


@pytest.fixture
def stores(db, settings):
    return JobStore(db, settings, UserStore(db)), JobStore(db, settings, UserStore(db))


def submit(store, user, *, session=None, key=None, task="Build a website"):
    return store.submit(user.id, MatchInput(task=task, agent="codex", session_id=session), key or str(uuid4()))


def result(skill="skill-a", cost=None):
    return ProviderOutcome(result=MatchResult(recommendations=[] if skill is None else [Recommendation(
        skill_id=skill, task_fit="Fits the task", taste_fit="Fits the profile", caveat=None)]),
        cost=cost, currency="unit" if cost is not None else None, input_tokens=100, output_tokens=50)


def expire(db, job_id, *, deadline=False):
    with db.db_sessions.begin() as session:
        job = session.get(MatchJobRecord, job_id)
        now = session.scalar(select(func.clock_timestamp()))
        if deadline:
            job.deadline_at = now - timedelta(seconds=1)
        else:
            job.lease_expires_at = now - timedelta(seconds=1)


def dispatch(store, job):
    return store.dispatch(job.id, job.lease_token, {"messages": [], "max_tokens": 10},
        Pricing(Decimal("0.000001"), Decimal("0.000001")))


def test_duplicate_request_survives_lost_response_and_counts_once(db, users, stores):
    first, second = stores
    one = submit(first, users[0], key="same")
    two = submit(second, users[0], key="same")
    assert one.latest_job.id == two.latest_job.id
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(MatchJobRecord)) == 1
        assert session.scalar(select(UserDailyUsageRecord.accepted_jobs)) == 1
    with pytest.raises(JobError) as error:
        submit(second, users[0], key="same", task="A different website")
    assert error.value.status_code == 409


def test_parallel_submit_is_scoped_to_user(db, users, stores):
    barrier = Barrier(2)
    def run(store, user):
        barrier.wait()
        try:
            return submit(store, user)
        except JobError as error:
            return error.status_code
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda pair: run(*pair), zip(stores, users)))
    assert all(item.status == "queued" for item in outcomes)
    stores[0].cancel(users[0].id, outcomes[0].latest_job.id)
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda store: run(store, users[0]), stores))
    assert sum(item == 409 for item in outcomes) == 1


def test_snapshot_is_frozen_at_acceptance(db, users, stores):
    store = stores[0]
    store.users.set_profile(users[0].id, Profile(communication="concise"))
    public = submit(store, users[0])
    store.users.set_profile(users[0].id, Profile(communication="detailed"))
    with db.db_sessions() as session:
        assert session.get(MatchJobRecord, public.latest_job.id).input["profile"]["communication"] == "concise"


def test_global_claim_cap_across_workers(db, settings, stores):
    with db.db_sessions.begin() as session:
        accounts = [UserRecord(github_id=i, login=str(i), avatar_url=None) for i in range(10, 16)]
        session.add_all(accounts)
        session.flush()
    for user in accounts:
        submit(stores[0], user)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda i: stores[i % 2].claim(str(i)), range(6)))
    assert sum(job is not None for job in claims) == settings.max_concurrent_jobs
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(MatchJobRecord).where(
            MatchJobRecord.status == "running")) == settings.max_concurrent_jobs


def test_expired_predispatch_can_retry_but_old_lease_cannot_publish(db, users, stores):
    public = submit(stores[0], users[0])
    old = stores[0].claim("worker-a")
    expire(db, old.id)
    new = stores[1].claim("worker-b")
    assert new.id == old.id and new.lease_token > old.lease_token
    assert not stores[0].finish(old.id, old.lease_token, result("stale"))
    assert stores[1].finish(new.id, new.lease_token, result())
    view = stores[0].users.get_match_session(users[0].id, public.id)
    assert [item.skill_id for item in view.recommendations] == ["skill-a"]


def test_expired_postdispatch_never_requeues_and_keeps_reservation(db, users, stores):
    submit(stores[0], users[0])
    job = stores[0].claim("worker-a")
    assert dispatch(stores[0], job)
    expire(db, job.id)
    assert stores[1].claim("worker-b") is None
    with db.db_sessions() as session:
        row = session.get(MatchJobRecord, job.id)
        assert row.status == "failed" and row.error_code == "provider_outcome_unknown"
        assert session.scalar(select(DailyUsageRecord.reserved)) > 0
        assert session.scalar(select(func.count()).select_from(ProviderAttemptRecord)) == 1


def test_budget_reservation_race_cannot_overspend(db, users, stores):
    for store, user in zip(stores, users):
        submit(store, user)
        store.settings = store.settings.model_copy(update={"daily_budget_units": Decimal("0.0015")})
    jobs = [store.claim(str(i)) for i, store in enumerate(stores)]
    barrier = Barrier(2)
    def run(pair):
        store, job = pair
        barrier.wait()
        return dispatch(store, job)
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(run, zip(stores, jobs)))
    assert sorted(outcomes) == [False, True]
    with db.db_sessions() as session:
        usage = session.scalar(select(DailyUsageRecord))
        assert usage.spent + usage.reserved <= Decimal("0.0015")


def test_cancelled_late_result_records_cost_without_replacing_new_batch(db, users, stores):
    store = stores[0]
    public = submit(store, users[0])
    old = store.claim("worker-a")
    assert dispatch(store, old)
    store.cancel(users[0].id, old.id)
    submit(store, users[0], session=public.id)
    new = store.claim("worker-b")
    assert store.finish(new.id, new.lease_token, result("new"))
    assert not store.finish(old.id, old.lease_token, result("old", Decimal("0.0002")))
    view = store.cancel(users[0].id, old.id)
    assert view.latest_job.id == new.id
    assert [item.skill_id for item in view.recommendations] == ["new"]
    with db.db_sessions() as session:
        usage = session.scalar(select(DailyUsageRecord))
        assert usage.spent == Decimal("0.0002") and usage.reserved == 0
    store.finish(old.id, old.lease_token, result("old", Decimal("0.0002")))
    with db.db_sessions() as session:
        assert session.scalar(select(DailyUsageRecord.spent)) == Decimal("0.0002")


def test_failed_continuation_preserves_batch_and_zero_result_clears(db, users, stores):
    store = stores[0]
    public = submit(store, users[0])
    job = store.claim("a")
    store.finish(job.id, job.lease_token, result())
    submit(store, users[0], session=public.id)
    failed = store.claim("b")
    store.finish(failed.id, failed.lease_token, ProviderOutcome(error_code="invalid_recommendations"))
    assert store.users.get_match_session(users[0].id, public.id).recommendations[0].skill_id == "skill-a"
    submit(store, users[0], session=public.id)
    empty = store.claim("c")
    store.finish(empty.id, empty.lease_token, result(None))
    assert store.users.get_match_session(users[0].id, public.id).recommendations == []
    with db.db_sessions() as session:
        assert set(session.scalars(select(MatchSessionSkillRecord.skill_id))) == {"skill-a"}


def test_claim_limit_and_deadline_are_not_reset(db, users, stores):
    store = stores[0]
    submit(store, users[0])
    original_deadline = None
    for _ in range(store.settings.max_job_claims):
        job = store.claim("worker")
        original_deadline = original_deadline or job.deadline_at
        assert job.deadline_at == original_deadline
        expire(db, job.id)
    assert store.claim("worker") is None
    with db.db_sessions() as session:
        assert session.get(MatchJobRecord, job.id).status == "failed"
    submit(store, users[0])
    job = store.claim("worker")
    expire(db, job.id, deadline=True)
    assert not store.heartbeat(job.id, job.lease_token)
    assert store.claim("worker") is None


class Retriever:
    def search(self, task, agent, excluded):
        return [{"id": "skill-a", "name": "Skill A"}]


class Enricher:
    async def enrich(self, skills):
        return skills


class Matcher:
    def __init__(self, outcome=None):
        self.calls = 0
        self.outcome = outcome or result()

    async def pricing(self, model):
        return Pricing(Decimal("0.000001"), Decimal("0.000001"))

    async def match(self, request, eligible):
        self.calls += 1
        return self.outcome


async def test_database_failure_after_response_retries_only_persistence(db, users, stores, monkeypatch):
    store = stores[0]
    public = submit(store, users[0])
    original = store.finish
    count = 0
    def flaky(*args):
        nonlocal count
        count += 1
        if count == 1:
            raise OperationalError("statement", {}, Exception())
        return original(*args)
    monkeypatch.setattr(store, "finish", flaky)
    matcher = Matcher()
    assert await Worker(store, Retriever(), Enricher(), matcher).run_once()
    assert matcher.calls == 1
    assert store.users.get_match_session(users[0].id, public.id).status == "completed"
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProviderAttemptRecord)) == 1


async def test_partial_provider_failure_and_unknown_cost_keep_reservation(db, users, stores):
    store = stores[0]
    public = submit(store, users[0])
    matcher = Matcher(ProviderOutcome(error_code="invalid_recommendations", input_tokens=50, output_tokens=10))
    await Worker(store, Retriever(), Enricher(), matcher).run_once()
    assert store.users.get_match_session(users[0].id, public.id).status == "failed"
    with db.db_sessions() as session:
        assert session.scalar(select(DailyUsageRecord.reserved)) > 0
        attempt = session.scalar(select(ProviderAttemptRecord))
        assert attempt.input_tokens == 50 and attempt.cost is None


async def test_two_workers_make_progress_for_two_users_at_once(users, stores):
    public = [submit(store, user) for store, user in zip(stores, users)]
    entered = 0
    both = asyncio.Event()
    class ConcurrentMatcher(Matcher):
        async def match(self, request, eligible):
            nonlocal entered
            entered += 1
            if entered == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return result()
    workers = [Worker(store, Retriever(), Enricher(), ConcurrentMatcher()) for store in stores]
    assert await asyncio.gather(*(worker.run_once() for worker in workers)) == [True, True]
    assert [store.users.get_match_session(user.id, task.id).status
        for store, user, task in zip(stores, users, public)] == ["completed", "completed"]


def test_cancel_and_finish_race_has_one_consistent_outcome(db, users, stores):
    public = submit(stores[0], users[0])
    job = stores[0].claim("worker")
    barrier = Barrier(2)
    def cancel():
        barrier.wait()
        return stores[0].cancel(users[0].id, job.id)
    def finish():
        barrier.wait()
        return stores[1].finish(job.id, job.lease_token, result())
    with ThreadPoolExecutor(2) as pool:
        one, two = pool.submit(cancel), pool.submit(finish)
        one.result(), two.result()
    final = stores[0].users.get_match_session(users[0].id, public.id)
    assert final.status in {"completed", "cancelled"}
    assert [item.skill_id for item in final.recommendations] == (["skill-a"] if final.status == "completed" else [])


def test_queue_expiry_and_daily_limit_do_not_charge_duplicates(db, users, stores):
    store = stores[0]
    store.settings = store.settings.model_copy(update={"matches_per_user_per_day": 1})
    public = submit(store, users[0], key="original")
    with db.db_sessions.begin() as session:
        row = session.get(MatchJobRecord, public.latest_job.id)
        row.created_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=601)
    assert store.claim("worker") is None
    assert submit(store, users[0], key="original").latest_job.status == "failed"
    with pytest.raises(JobError) as error:
        submit(store, users[0])
    assert error.value.status_code == 429


async def test_shutdown_after_dispatch_recovers_without_second_provider_call(db, users, stores):
    store = stores[0]
    public = submit(store, users[0])
    started = asyncio.Event()
    class HangingMatcher(Matcher):
        async def match(self, request, eligible):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()
    matcher = HangingMatcher()
    worker = Worker(store, Retriever(), Enricher(), matcher)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    expire(db, public.latest_job.id)
    assert stores[1].claim("replacement") is None
    assert matcher.calls == 1
    view = store.users.get_match_session(users[0].id, public.id)
    assert view.latest_job.error_code == "provider_outcome_unknown"


def test_cross_user_cancellation_and_invalid_ids_are_not_found(users, stores):
    public = submit(stores[0], users[0])
    for job_id in (public.latest_job.id, "invalid"):
        with pytest.raises(JobError) as error:
            stores[1].cancel(users[1].id, job_id)
        assert error.value.status_code == 404
    with pytest.raises(JobError) as error:
        submit(stores[1], users[1], session="invalid")
    assert error.value.status_code == 404
    assert stores[0].users.get_match_session(users[0].id, public.id).status == "queued"


async def test_continuation_can_repeat_unreviewed_skills_but_excludes_feedback(db, users, stores):
    store = stores[0]
    public = submit(store, users[0])
    job = store.claim("first")
    initial = result("skill-a")
    initial.result.recommendations.append(result("skill-b").result.recommendations[0])
    store.finish(job.id, job.lease_token, initial)
    store.users.add_feedback(users[0].id, FeedbackInput(
        session_id=public.id, skill_id="skill-a", action="pass"))
    submit(store, users[0], session=public.id)
    class ReviewedRetriever:
        def search(self, task, agent, excluded):
            assert excluded == {"skill-a"}
            return [{"id": "skill-b", "name": "Skill B"}]
    await Worker(store, ReviewedRetriever(), Enricher(), Matcher(result("skill-b"))).run_once()
    continued = store.users.get_match_session(users[0].id, public.id)
    assert continued.status == "completed"
    assert [item.skill_id for item in continued.recommendations] == ["skill-b"]


@pytest.mark.parametrize("during_provider", [False, True])
async def test_lost_authority_stops_local_work_and_never_publishes(db, users, stores, during_provider):
    store = stores[0]
    store.settings = store.settings.model_copy(update={"job_heartbeat_seconds": 0.01})
    public = submit(store, users[0])
    entered = asyncio.Event()
    stopped = asyncio.Event()
    async def hold():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    class BlockingEnricher(Enricher):
        async def enrich(self, skills):
            if not during_provider:
                await hold()
            return skills
    class BlockingMatcher(Matcher):
        async def match(self, request, eligible):
            self.calls += 1
            await hold()
    matcher = BlockingMatcher()
    running = asyncio.create_task(Worker(store, Retriever(), BlockingEnricher(), matcher).run_once())
    await asyncio.wait_for(entered.wait(), 2)
    stores[1].cancel(users[0].id, public.latest_job.id)
    await asyncio.wait_for(stopped.wait(), 2)
    with pytest.raises(asyncio.CancelledError):
        await running
    final = store.users.get_match_session(users[0].id, public.id)
    assert final.status == "cancelled" and final.recommendations == []
    assert matcher.calls == (1 if during_provider else 0)
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProviderAttemptRecord)) == (1 if during_provider else 0)


async def test_expired_work_is_reaped_while_model_prices_are_unavailable(db, users, stores):
    store = stores[0]
    running_public = submit(store, users[0])
    running = store.claim("dead-worker")
    dispatch(store, running)
    expire(db, running.id)
    queued_public = submit(store, users[1])
    with db.db_sessions.begin() as session:
        job = session.get(MatchJobRecord, queued_public.latest_job.id)
        job.created_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=601)
    class UnavailableMatcher(Matcher):
        async def pricing(self, model):
            worker.stop()
            raise ProviderError("pricing_unavailable")
    worker = Worker(store, Retriever(), Enricher(), UnavailableMatcher())
    await worker.run()
    assert store.users.get_match_session(users[0].id, running_public.id).latest_job.error_code == "provider_outcome_unknown"
    assert store.users.get_match_session(users[1].id, queued_public.id).latest_job.error_code == "job_expired"


def test_worker_logging_hides_transport_debug_payloads(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    import skinder.worker as module
    async def fake_serve(settings):
        logging.getLogger("openai").debug("private provider input")
        logging.getLogger("httpx").debug("private request headers")
        logging.getLogger("skinder.worker").info("worker status")
    monkeypatch.setattr(module, "load_settings", lambda role: SimpleNamespace(log_level="DEBUG"))
    monkeypatch.setattr(module, "serve", fake_serve)
    old = {name: logging.getLogger(name).level for name in (
        "openai", "httpx", "httpcore", "httpx2", "authlib", "sqlalchemy.engine")}
    try:
        with caplog.at_level(logging.DEBUG):
            module.main()
        assert "worker status" in caplog.text
        assert "private provider input" not in caplog.text
        assert "private request headers" not in caplog.text
    finally:
        for name, level in old.items():
            logging.getLogger(name).setLevel(level)


async def test_quote_heavy_sources_trim_to_full_provider_envelope(db, users, stores):
    import json

    store = stores[0]
    store.settings = store.settings.model_copy(update={"max_prompt_bytes": 8192})
    profile = Profile(communication="concise", output_style='Use "examples" and C:\\paths')
    store.users.set_profile(users[0].id, profile)
    public = submit(store, users[0], task='Explain "quoted" source paths and \\escapes')
    class QuotedRetriever:
        def search(self, task, agent, excluded):
            return [{"id": "skill-a", "name": "Skill A", "source_excerpt": '\\"' * 1400}]
    matcher = Matcher()
    await Worker(store, QuotedRetriever(), Enricher(), matcher).run_once()
    final = store.users.get_match_session(users[0].id, public.id)
    assert final.status == "completed" and matcher.calls == 1
    with db.db_sessions() as session:
        payload = session.get(MatchJobRecord, public.latest_job.id).provider_request
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= store.settings.max_prompt_bytes
    supplied = json.loads(payload["messages"][1]["content"])
    assert supplied["task"] == public.task
    assert supplied["profile"] == profile.model_dump()
    assert supplied["eligible_skills"][0]["source_excerpt"] == ""
    assert supplied["eligible_skills"][0]["source_truncated"] is True
