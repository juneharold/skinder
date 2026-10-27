"""Independent worker process with leases, fencing, and one provider attempt per job."""
import asyncio
import contextlib
import json
import logging
import signal
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .database import Database
from .diagnostics import configure_logging
from .jobs import JobStore
from .models import MatchResult
from .provider import LetsurMatcher, ProviderError, ProviderOutcome, build_request, request_size
from .settings import load_settings
from .sources import bounded_request
from .work import run_sync

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, jobs: JobStore, retriever, enricher, matcher, *, worker_id=None):
        self.jobs, self.retriever, self.enricher, self.matcher = jobs, retriever, enricher, matcher
        self.settings = jobs.settings
        self.id = worker_id or str(uuid4())
        self.stopping = asyncio.Event()
        self.active: set[asyncio.Task] = set()

    def stop(self):
        self.stopping.set()

    async def _heartbeat(self, job, execution):
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_seconds)
            try:
                if not await run_sync(self.jobs.heartbeat, job.id, job.lease_token):
                    execution.cancel()
                    return
            except SQLAlchemyError:
                # A failed renewal cannot extend authority; the database lease fences writes.
                logger.warning("job_heartbeat_failed job_id=%s", job.id)

    async def _persist(self, job, outcome, timings):
        # Retry only the transaction after a received response. The provider call is
        # outside this loop and the durable dispatch marker forbids replay.
        while True:
            try:
                return await run_sync(self.jobs.finish, job.id, job.lease_token, outcome, timings)
            except SQLAlchemyError:
                logger.warning("job_persistence_retry job_id=%s", job.id)
                await asyncio.sleep(min(1, self.settings.job_heartbeat_seconds))
                try:
                    if not await run_sync(self.jobs.remaining, job.id, job.lease_token):
                        # One final transaction can still record usage for a cancelled job.
                        return await run_sync(self.jobs.finish, job.id, job.lease_token, outcome, timings)
                except SQLAlchemyError:
                    return False

    async def execute(self, job):
        heartbeat = asyncio.create_task(self._heartbeat(job, asyncio.current_task()))
        timings = {}
        dispatched = False
        stage = "retrieval"
        start = monotonic()
        try:
            remaining = await run_sync(self.jobs.remaining, job.id, job.lease_token)
            if remaining <= 0:
                return
            async with asyncio.timeout(remaining):
                candidates = await run_sync(self.retriever.search, job.input["task"],
                    job.input["agent"], set(job.input["seen_ids"]))
                timings[stage] = round(monotonic() - start, 3)
                stage, start = "enrichment", monotonic()
                enriched = await self.enricher.enrich(candidates[:40])
                timings[stage] = round(monotonic() - start, 3)
                stage, start = "provider", monotonic()
                # Trim against the complete serialized envelope, including escaped message
                # content. The exact bounded payload is saved before paid dispatch.
                instructions = job.input.get("instructions")
                data = bounded_request(job.input["task"], job.input["profile"],
                    job.input["feedback"], enriched, self.settings,
                    request_size=lambda value: request_size(value, job.model, self.settings, instructions))
                if not data["eligible_skills"]:
                    outcome = ProviderOutcome(result=MatchResult(recommendations=[]))
                else:
                    request = build_request(data, job.model, self.settings, instructions)
                    pricing = await self.matcher.pricing(job.model)
                    if not await run_sync(self.jobs.dispatch, job.id, job.lease_token, request, pricing):
                        return
                    dispatched = True
                    outcome = await self.matcher.match(request, {item["id"] for item in data["eligible_skills"]})
                timings[stage] = round(monotonic() - start, 3)
            await self._persist(job, outcome, timings)
        except asyncio.CancelledError:
            # Shutdown deliberately leaves the marker and lease for durable recovery.
            raise
        except TimeoutError:
            await self._persist(job, ProviderOutcome(error_code=(
                "provider_outcome_unknown" if dispatched else "job_timeout"), unknown=dispatched), timings)
        except ProviderError as exc:
            await self._persist(job, ProviderOutcome(error_code=exc.code, unknown=exc.unknown,
                error_ref=exc.error_ref), timings)
        except Exception as exc:  # noqa: BLE001 - isolate jobs without logging private exception text
            logger.error("job_failed job_id=%s stage=%s exception=%s", job.id, stage, type(exc).__name__)
            await self._persist(job, ProviderOutcome(error_code=(
                "provider_outcome_unknown" if dispatched else "matching_failed"), unknown=dispatched), timings)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            logger.info("job_finished job_id=%s timings=%s", job.id, json.dumps(timings))

    async def run_once(self):
        """Claim and complete one job; convenient for process probes and tests."""
        job = await run_sync(self.jobs.claim, self.id)
        if job is None:
            return False
        await self.execute(job)
        return True

    async def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    await run_sync(self.jobs.reap)
                    await self.matcher.pricing(self.settings.letsur_model)
                    await run_sync(self.jobs.worker_heartbeat, self.id, True)
                    self.active = {task for task in self.active if not task.done()}
                    while len(self.active) < self.settings.max_concurrent_jobs and not self.stopping.is_set():
                        job = await run_sync(self.jobs.claim, self.id)
                        if job is None:
                            break
                        task = asyncio.create_task(self.execute(job))
                        self.active.add(task)
                except Exception as exc:  # noqa: BLE001 - isolate jobs without logging private exception text
                    logger.error("worker_poll_failed exception=%s", type(exc).__name__)
                    with contextlib.suppress(SQLAlchemyError):
                        await run_sync(self.jobs.worker_heartbeat, self.id, False)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), self.settings.worker_poll_seconds)
        finally:
            with contextlib.suppress(SQLAlchemyError):
                await run_sync(self.jobs.worker_heartbeat, self.id, False)
            if self.active:
                done, pending = await asyncio.wait(self.active, timeout=self.settings.shutdown_grace_seconds)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)


async def serve(settings):
    from .catalog import CatalogStore
    from .retrieval import HybridRetriever
    from .sources import SourceEnricher
    from .user import UserStore

    database = Database(settings)
    database.check_schema()
    catalog = CatalogStore(database, settings)
    matcher = LetsurMatcher(settings)
    enricher = SourceEnricher(catalog, settings)
    worker = Worker(JobStore(database, settings, UserStore(database)),
        HybridRetriever(catalog, settings), enricher, matcher)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.run()
    finally:
        await matcher.close()
        database.close()


def main():
    settings = load_settings("worker")
    configure_logging(settings.log_level)
    asyncio.run(serve(settings))
