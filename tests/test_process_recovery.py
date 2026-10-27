"""Real process death and process-shared PostgreSQL coordination, with no network calls."""
import asyncio
import json
import multiprocessing
import os
import queue
from contextlib import contextmanager
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from sqlalchemy import func, select

from skinder.database import Database
from skinder.jobs import JobStore
from skinder.models import MatchInput, MatchResult, Recommendation
from skinder.orm import MatchJobRecord, ProviderAttemptRecord, UserDailyUsageRecord, UserRecord
from skinder.provider import Pricing, ProviderOutcome
from skinder.settings import Settings
from skinder.user import UserStore
from skinder.worker import Worker


class ProcessRetriever:
    def search(self, task, agent, excluded):
        return [{"id": "process-skill", "name": "Process skill"}]


class ProcessEnricher:
    def __init__(self, mode, events):
        self.mode, self.events = mode, events

    async def enrich(self, skills):
        if self.mode == "before":
            self.events.put(("before", os.getpid()))
            await asyncio.Event().wait()
        return skills


class ProcessMatcher:
    def __init__(self, mode, events, calls):
        self.mode, self.events, self.calls = mode, events, calls

    async def pricing(self, model):
        return Pricing(Decimal("0.000001"), Decimal("0.000001"))

    def record_call(self, request):
        # This file is the external service's durable call log. Flush before announcing
        # the boundary, so killing a worker cannot erase evidence that it dispatched.
        with open(self.calls, "a") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "model": request["model"]}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def match(self, request, eligible):
        await asyncio.to_thread(self.record_call, request)
        self.events.put(("provider", os.getpid()))
        if self.mode == "after":
            await asyncio.Event().wait()
        return ProviderOutcome(result=MatchResult(recommendations=[Recommendation(
            skill_id="process-skill", task_fit="Fits", taste_fit="Fits", caveat=None)]),
            cost=Decimal("0.0001"), currency="unit")


def child_settings(settings):
    values = settings.model_dump(mode="json")
    values["database_url"] = settings.database_url.get_secret_value()
    values["letsur_api_key"] = "unused-test-key"
    values.update(job_lease_seconds=0.5, job_heartbeat_seconds=0.05, worker_poll_seconds=0.02)
    return values


def worker_child(values, schema, mode, events, calls, target_job=None, barrier=None):
    """Spawn starts a fresh interpreter and creates its own connection pool."""
    database = Database(Settings(_env_file=None, **values), schema=schema)
    store = JobStore(database, Settings(_env_file=None, **values), UserStore(database))
    async def run():
        worker = Worker(store, ProcessRetriever(), ProcessEnricher(mode, events),
            ProcessMatcher(mode, events, calls))
        if barrier:
            barrier.wait(timeout=10)
        announced_idle = False
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if await worker.run_once():
                events.put(("completed", os.getpid()))
                return
            if not announced_idle:
                events.put(("idle", os.getpid()))
                announced_idle = True
            if target_job:
                with database.db_sessions() as session:
                    status = session.get(MatchJobRecord, target_job).status
                if status in {"failed", "cancelled", "completed"}:
                    events.put(("terminal", os.getpid(), status))
                    return
            await asyncio.sleep(0.02)
        raise AssertionError("Worker did not recover a job within the test deadline")
    try:
        asyncio.run(run())
    finally:
        database.close()


def submit_child(values, schema, user_id, other_user_id, key, events, barrier):
    database = Database(Settings(_env_file=None, **values), schema=schema)
    try:
        store = JobStore(database, Settings(_env_file=None, **values), UserStore(database))
        barrier.wait(timeout=10)
        public = store.submit(user_id, MatchInput(task="Process submission", agent="codex"), key)
        events.put((public.id, public.latest_job.id,
            store.users.get_match_session(other_user_id, public.id) is None))
    finally:
        database.close()


@contextmanager
def children():
    processes = []
    try:
        yield processes
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
        for process in processes:
            process.join(timeout=5)
            assert not process.is_alive(), "Child process did not stop"
            process.close()


def start(processes, context, target, args):
    process = context.Process(target=target, args=args)
    process.start()
    processes.append(process)
    return process


def wait_for(events, kind, timeout=10):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            event = events.get(timeout=max(0.01, deadline - monotonic()))
        except queue.Empty:
            break
        if event[0] == kind:
            return event
    raise AssertionError(f"Child did not reach {kind}")


def call_count(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_killed_worker_before_dispatch_is_reclaimed_by_new_process(db, settings, users, tmp_path):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    calls = tmp_path / "provider-calls.jsonl"
    values = child_settings(settings)
    store = JobStore(db, settings, UserStore(db))
    public = store.submit(users[0].id, MatchInput(task="Recover my match", agent="codex"), "before")
    with children() as processes:
        original = start(processes, context, worker_child, (values, db.schema, "before", events, calls))
        wait_for(events, "before")
        original.kill()
        original.join(timeout=5)
        assert call_count(calls) == 0
        replacement = start(processes, context, worker_child,
            (values, db.schema, "complete", events, calls, public.latest_job.id))
        wait_for(events, "completed")
        replacement.join(timeout=5)
        assert replacement.exitcode == 0
    final = store.users.get_match_session(users[0].id, public.id)
    assert final.status == "completed"
    assert [item.skill_id for item in final.recommendations] == ["process-skill"]
    assert call_count(calls) == 1
    with db.db_sessions() as session:
        assert session.get(MatchJobRecord, public.latest_job.id).claim_count == 2
        assert session.scalar(select(func.count()).select_from(ProviderAttemptRecord)) == 1
    events.close()


def test_killed_worker_after_dispatch_never_calls_provider_again(db, settings, users, tmp_path):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    calls = tmp_path / "provider-calls.jsonl"
    values = child_settings(settings)
    store = JobStore(db, settings, UserStore(db))
    public = store.submit(users[0].id, MatchInput(task="Recover my match", agent="codex"), "after")
    with children() as processes:
        original = start(processes, context, worker_child, (values, db.schema, "after", events, calls))
        wait_for(events, "provider")
        original.kill()
        original.join(timeout=5)
        replacement = start(processes, context, worker_child,
            (values, db.schema, "complete", events, calls, public.latest_job.id))
        assert wait_for(events, "terminal")[2] == "failed"
        replacement.join(timeout=5)
        assert replacement.exitcode == 0
    final = store.users.get_match_session(users[0].id, public.id)
    assert final.latest_job.error_code == "provider_outcome_unknown"
    assert final.recommendations == [] and call_count(calls) == 1
    with db.db_sessions() as session:
        job = session.get(MatchJobRecord, public.latest_job.id)
        assert job.claim_count == 1 and job.reserved_units > 0 and not job.reservation_reconciled
        assert session.scalar(select(func.count()).select_from(ProviderAttemptRecord)) == 1
    events.close()


def test_processes_claim_distinct_jobs_and_enforce_shared_capacity(db, settings, users, tmp_path):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    calls = tmp_path / "provider-calls.jsonl"
    values = child_settings(settings)
    # Give spawned interpreters time to start before testing the shared claim boundary.
    values.update(max_concurrent_jobs=2, job_lease_seconds=5, job_heartbeat_seconds=0.1)
    barrier = context.Barrier(3)
    with db.db_sessions.begin() as session:
        third = UserRecord(github_id=2003, login="third", avatar_url=None)
        session.add(third)
        session.flush()
    store = JobStore(db, settings, UserStore(db))
    for user in (*users, third):
        store.submit(user.id, MatchInput(task="Parallel process matching", agent="codex"), str(uuid4()))
    with children() as processes:
        for _ in range(3):
            start(processes, context, worker_child, (values, db.schema, "after", events, calls, None, barrier))
        providers, idle = set(), set()
        deadline = monotonic() + 10
        while len(providers) < 2 or not idle:
            event = events.get(timeout=max(0.01, deadline - monotonic()))
            if event[0] == "provider":
                providers.add(event[1])
            elif event[0] == "idle":
                idle.add(event[1])
            assert monotonic() < deadline
        with db.db_sessions() as session:
            running = list(session.scalars(select(MatchJobRecord).where(MatchJobRecord.status == "running")))
            assert len(running) == 2 and len({job.worker_id for job in running}) == 2
            assert session.scalar(select(func.count()).select_from(MatchJobRecord).where(
                MatchJobRecord.status == "queued")) == 1
        assert call_count(calls) == 2
    events.close()


def test_two_submission_processes_share_idempotence_and_enforce_ownership(db, settings, users):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    barrier = context.Barrier(2)
    values = child_settings(settings)
    with children() as processes:
        for _ in range(2):
            start(processes, context, submit_child,
                (values, db.schema, users[0].id, users[1].id, "shared-key", events, barrier))
        first, second = events.get(timeout=10), events.get(timeout=10)
        assert first == second and first[2] is True
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(MatchJobRecord)) == 1
        assert session.scalar(select(UserDailyUsageRecord.accepted_jobs)) == 1
    events.close()
