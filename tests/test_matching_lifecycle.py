import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from test_catalog_store import example_skill

from skinder.app import create_app
from skinder.catalog import CatalogStore
from skinder.matcher import terminate
from skinder.matching import MatchServiceError
from skinder.models import CodexStatus, MatchInput
from skinder.ownership import DatabaseOwnership
from skinder.sources import SourceEnricher
from skinder.work import run_sync


# Explicit gates place cancellation inside an active match without depending on machine speed.
class WaitingMatcher:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def health(self):
        return CodexStatus(installed=True, authenticated=True, message="Ready")

    async def match(self, request):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return '{"recommendations": []}'


class MetadataOnly:
    async def enrich(self, skills):
        from skinder.sources import match_metadata
        return [match_metadata(skill) for skill in skills]


async def test_termination_reaps_child_that_ignores_graceful_shutdown():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"ready\n"
        await asyncio.wait_for(terminate(process), 5)
        assert process.returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@asynccontextmanager
async def running_app(backend, **options):
    options.setdefault("enricher", MetadataOnly())
    app = create_app(
        user_db_path=backend.user_db_path, catalog_db_path=backend.root / "catalog.sqlite3",
        **options,
    )
    async with app.router.lifespan_context(app):
        yield app


async def wait_until_finished(service, session_id):
    async with asyncio.timeout(3):
        while True:
            session = await run_sync(service.get_session, session_id)
            if session.status != "running":
                return session
            await asyncio.sleep(0.005)


async def test_simultaneous_start_and_lost_response_leave_one_owned_job(backend, monkeypatch):
    matcher = WaitingMatcher()
    async with running_app(backend, matcher=matcher) as app:
        service = app.state.match_service
        entered, release = threading.Event(), threading.Event()
        original = app.state.user_store.get_profile

        def blocked_profile():
            entered.set()
            assert release.wait(3)
            return original()

        monkeypatch.setattr(app.state.user_store, "get_profile", blocked_profile)
        value = MatchInput(task="Review changes", agent="codex")
        first = asyncio.create_task(service.start(value))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(service.start(value))
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(MatchServiceError) as error:
            await second
        assert error.value.status_code == 409
        await asyncio.wait_for(matcher.entered.wait(), 2)
        session = await run_sync(app.state.user_store.get_current_match_session)
        assert session.status == "running"
        assert matcher.calls == 1
        cancelled = await service.cancel(session.id)
        assert cancelled.status == "cancelled"
        matcher.release.set()
        assert (await run_sync(service.get_session, session.id)).status == "cancelled"


async def test_cancelled_caller_still_finishes_cancellation(backend, monkeypatch):
    matcher = WaitingMatcher()
    async with running_app(backend, matcher=matcher) as app:
        service = app.state.match_service
        session = await service.start(MatchInput(task="Review changes", agent="codex"))
        await asyncio.wait_for(matcher.entered.wait(), 2)
        entered, release = threading.Event(), threading.Event()
        original = app.state.user_store.finish_match_session

        def blocked_finish(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(app.state.user_store, "finish_match_session", blocked_finish)
        cancellation = asyncio.create_task(service.cancel(session.id))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            cancellation.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancellation
        assert (await run_sync(service.get_session, session.id)).status == "cancelled"
        next_session = await service.start(MatchInput(task="Review another task", agent="codex"))
        assert next_session.id != session.id
        await service.cancel(next_session.id)


async def test_shutdown_waits_for_retrieval_before_releasing_database(backend):
    entered, release = threading.Event(), threading.Event()

    class BlockingRetriever:
        def search(self, task, agent, excluded):
            entered.set()
            assert release.wait(3)
            return []

    app = create_app(user_db_path=backend.user_db_path, retriever=BlockingRetriever())
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    session = await app.state.match_service.start(MatchInput(task="Review changes", agent="codex"))
    assert await asyncio.to_thread(entered.wait, 2)
    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    probe = DatabaseOwnership(backend.user_db_path)
    try:
        await asyncio.sleep(0.03)
        assert not shutdown.done()
        with pytest.raises(RuntimeError):
            probe.acquire()
    finally:
        release.set()
        await shutdown
    probe.acquire()
    probe.release()
    with sqlite3.connect(backend.user_db_path) as db:
        status, error = db.execute(
            "SELECT status, error FROM match_sessions WHERE id=?", (session.id,),
        ).fetchone()
    assert status == "failed" and "shutdown" in error


@pytest.mark.parametrize("stage,exception", [
    ("matcher", RuntimeError), ("matcher", ValueError),
    ("retrieval", ValueError), ("enrichment", ValueError),
])
async def test_failure_diagnostics_keep_context_without_sensitive_values(
    backend, caplog, stage, exception,
):
    def fail():
        raise exception("private-task access_token=secret provider stderr SQL parameters")

    class BrokenMatcher(WaitingMatcher):
        async def match(self, request):
            fail()

    class BrokenRetriever:
        def search(self, task, agent, excluded):
            fail()

    class BrokenEnricher:
        async def enrich(self, skills):
            fail()

    options = {"matcher": BrokenMatcher()}
    if stage == "retrieval":
        options["retriever"] = BrokenRetriever()
    if stage == "enrichment":
        options["enricher"] = BrokenEnricher()
    async with running_app(backend, **options) as app:
        service = app.state.match_service
        session = await service.start(MatchInput(task="private-task Review changes", agent="codex"))
        result = await wait_until_finished(service, session.id)
        assert result.status == "failed"
        assert result.error is not None
    assert session.id in caplog.text
    assert stage in caplog.text and exception.__name__ in caplog.text
    assert "test_matching_lifecycle.py" in caplog.text
    diagnostics = caplog.text + result.error
    assert all(value not in diagnostics for value in ("private-task", "secret", "stderr", "parameters"))


async def test_source_cache_failure_logs_underlying_error_without_sensitive_values(backend, caplog):
    catalog = CatalogStore(backend.root / "catalog.sqlite3")
    skill = example_skill(99)
    catalog.upsert(skill)
    with catalog.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_source_cache BEFORE INSERT ON source_cache "
            "BEGIN SELECT RAISE(ABORT, 'private-task access_token=secret provider stderr'); END"
        )

    class SingleSkillRetriever:
        def search(self, task, agent, excluded):
            return [skill]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="private-source-text"))
    try:
        async with running_app(
            backend, matcher=WaitingMatcher(), retriever=SingleSkillRetriever(),
            enricher=SourceEnricher(catalog, transport=transport),
        ) as app:
            service = app.state.match_service
            session = await service.start(MatchInput(task="Review changes", agent="codex"))
            result = await wait_until_finished(service, session.id)
            assert result.status == "failed" and result.error is not None
        assert "IntegrityError" in caplog.text and "sources.py" in caplog.text
        assert session.id in caplog.text and "enrichment" in caplog.text
        diagnostics = caplog.text + result.error
        assert all(value not in diagnostics for value in (
            "private-task", "secret", "stderr", "private-source-text",
        ))
    finally:
        catalog.close()


async def test_database_contention_does_not_block_other_requests(backend):
    async with (
        running_app(backend, matcher=WaitingMatcher()) as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742",
        ) as client,
    ):
        with sqlite3.connect(backend.user_db_path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            pending = asyncio.create_task(client.put("/api/profile", json={"output_style": "brief"}))
            try:
                await asyncio.sleep(0.03)
                assert not pending.done()
                response = await asyncio.wait_for(client.get("/api/matches/current"), 0.5)
                assert response.status_code == 200 and response.json() is None
            finally:
                blocker.rollback()
                result = await pending
            assert result.status_code == 200
            assert result.json()["output_style"] == "brief"

async def test_cancelled_thread_failure_preserves_cancellation():
    entered, release = threading.Event(), threading.Event()
    finished = threading.Event()

    def failing_work():
        entered.set()
        assert release.wait(3)
        finished.set()
        raise RuntimeError("private worker error")

    task = asyncio.create_task(run_sync(failing_work))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_process_lock_resolves_symlinks_and_can_be_reacquired(tmp_path):
    database = tmp_path / "user.sqlite3"
    database.touch()
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(database)
    owner = DatabaseOwnership(database)
    owner.acquire()
    code = (
        "from pathlib import Path; from skinder.ownership import DatabaseOwnership; "
        "import sys; owner = DatabaseOwnership(Path(sys.argv[1])); owner.acquire(); owner.release()"
    )
    try:
        rejected = subprocess.run([sys.executable, "-c", code, str(alias)], capture_output=True, check=False)
        assert rejected.returncode != 0
        assert b"already using this user database" in rejected.stderr
    finally:
        owner.release()
    accepted = subprocess.run([sys.executable, "-c", code, str(alias)], capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr.decode()


async def test_second_app_cannot_recover_active_job_and_startup_failure_releases_lock(backend, monkeypatch):
    matcher = WaitingMatcher()
    async with running_app(backend, matcher=matcher) as app:
        session = await app.state.match_service.start(MatchInput(task="Review changes", agent="codex"))
        with pytest.raises(RuntimeError, match="already using"):
            async with running_app(backend):
                pytest.fail("Second server acquired the database")
        assert (await run_sync(app.state.match_service.get_session, session.id)).status == "running"
        await app.state.match_service.cancel(session.id)

    def failed_catalog(*args, **kwargs):
        raise RuntimeError("Cannot initialize catalog")

    with monkeypatch.context() as patch:
        patch.setattr("skinder.app.CatalogStore", failed_catalog)
        with pytest.raises(RuntimeError, match="Cannot initialize"):
            async with running_app(backend):
                pytest.fail("Startup should fail")
    async with running_app(backend) as app:
        assert (await run_sync(app.state.match_service.get_session, session.id)).status == "cancelled"


async def test_late_match_result_cannot_overwrite_cancellation(backend):
    class LateMatcher(WaitingMatcher):
        async def match(self, request):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                return '{"recommendations": []}'

    matcher = LateMatcher()
    async with running_app(backend, matcher=matcher) as app:
        service = app.state.match_service
        session = await service.start(MatchInput(task="Review changes", agent="codex"))
        await asyncio.wait_for(matcher.entered.wait(), 2)
        assert (await service.cancel(session.id)).status == "cancelled"
        assert (await run_sync(service.get_session, session.id)).status == "cancelled"


async def test_shutdown_waits_for_cancelled_request_write(backend, monkeypatch):
    app = create_app(user_db_path=backend.user_db_path)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    entered, release = threading.Event(), threading.Event()
    closed = threading.Event()
    users = app.state.user_store
    original = users.set_profile

    def blocked_write(profile):
        entered.set()
        assert release.wait(3)
        assert not closed.is_set()
        return original(profile)

    monkeypatch.setattr(users, "set_profile", blocked_write)
    # SQLAlchemy's disposal event observes the actual resource lifecycle.
    from sqlalchemy import event
    event.listen(users.engine, "engine_disposed", lambda engine: closed.set())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742",
    ) as client:
        request = asyncio.create_task(client.put("/api/profile", json={"output_style": "brief"}))
        assert await asyncio.to_thread(entered.wait, 2)
        request.cancel()
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        try:
            await asyncio.sleep(0.02)
            assert not shutdown.done() and not closed.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            await shutdown
    assert closed.is_set()
    with sqlite3.connect(backend.user_db_path) as db:
        assert '"output_style":"brief"' in db.execute("SELECT data FROM profile").fetchone()[0]
