"""Matching lifecycle for the single local user."""

import asyncio
import logging
import traceback
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .catalog import CatalogStore
from .matcher import Matcher, MatchError
from .models import Agent, Engine, MatchInput, MatchResult, MatchSession, Skill
from .retrieval import RetrievalStatus
from .sources import bounded_request, match_metadata
from .user import UserStore
from .work import run_sync, wait_for_work

logger = logging.getLogger(__name__)


class Retriever(Protocol):
    def search(self, task: str, agent: Agent, excluded: set[str]) -> list[Skill]: ...
    def status(self) -> RetrievalStatus: ...


class Enricher(Protocol):
    async def enrich(self, skills: list[Skill]) -> list[dict[str, Any]]: ...


class InvalidRecommendations(Exception):
    pass


class MatchServiceError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class MatchService:
    def __init__(
        self, users: UserStore, catalog: CatalogStore, matchers: dict[Engine, Matcher],
        retriever: Retriever, enricher: Enricher,
    ) -> None:
        self.users = users
        self.catalog = catalog
        self.matchers = matchers
        self.retriever = retriever
        self.enricher = enricher
        # Ownership excludes other servers; this lock serializes jobs within this server.
        self._lock = asyncio.Lock()
        self._active: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._closing = False

    def get_session(self, session_id: str) -> MatchSession:
        session = self.users.get_match_session(session_id)
        if session is None:
            raise MatchServiceError(404, "Task session not found.")
        return session

    def _prepare(self, value: MatchInput) -> tuple[MatchSession, dict[str, Any], list[dict[str, Any]]]:
        if value.session_id:
            previous = self.get_session(value.session_id)
            if (previous.task, previous.agent, previous.engine) != (
                value.task, value.agent, value.engine,
            ):
                raise MatchServiceError(
                    409, "A continuation must use the same task, target agent, and engine.",
                )
        profile = self.users.get_profile().model_dump()
        # A task can target a different agent from the saved profile default.
        profile["agent"] = value.agent
        feedback = []
        for event in self.users.get_preference_events(value.agent):
            skill = self.catalog.get(event["skill_id"])
            if skill:
                feedback.append({**event, "skill": match_metadata(skill)})
        session = self.users.start_match_session(
            value.task, value.agent, value.session_id, engine=value.engine,
        )
        assert session is not None  # The continuation was validated before this write.
        return session, profile, feedback

    async def start(self, value: MatchInput) -> MatchSession:
        # The accepted operation owns task creation even if the HTTP caller goes away.
        return await wait_for_work(asyncio.create_task(self._start(value)))

    async def _start(self, value: MatchInput) -> MatchSession:
        async with self._lock:
            if self._closing:
                raise MatchServiceError(503, "Matching was interrupted by shutdown.")
            if self._active is not None and not self._active.done():
                raise MatchServiceError(409, "A match is already running. Wait for it or cancel it first.")
            if value.engine not in self.matchers:
                raise MatchServiceError(503, "The selected recommendation engine is unavailable.")
            session, profile, feedback = await run_sync(self._prepare, value)
            self._session_id = session.id
            self._active = asyncio.create_task(self._run(session, profile, feedback))
            return session

    async def cancel(self, session_id: str) -> MatchSession:
        return await wait_for_work(asyncio.create_task(self._cancel(session_id)))

    async def _cancel(self, session_id: str) -> MatchSession:
        async with self._lock:
            if self._closing:
                raise MatchServiceError(503, "Matching was interrupted by shutdown.")
            session = await run_sync(self.get_session, session_id)
            if session.status == "running":
                # Persist cancellation first so a late result cannot replace it.
                await run_sync(self.users.finish_match_session, session_id, "cancelled")
                if self._session_id == session_id:
                    await self._stop_active()
            return await run_sync(self.get_session, session_id)

    async def _stop_active(self) -> None:
        if self._active is None or self._active.done():
            return
        self._active.cancel()
        try:
            await self._active
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        # Reject queued starts before yielding to the shutdown task and waiting for the lock.
        self._closing = True
        await wait_for_work(asyncio.create_task(self._shutdown()))

    async def _shutdown(self) -> None:
        async with self._lock:
            if self._active is None or self._active.done():
                return
            assert self._session_id is not None
            try:
                await run_sync(
                    self.users.finish_match_session, self._session_id, "failed",
                    error="Matching was interrupted by shutdown.",
                )
            finally:
                # A failed status write must not leave the matcher running.
                await self._stop_active()

    def _search(self, session: MatchSession) -> list[Skill]:
        return self.retriever.search(
            session.task, session.agent, self.users.get_seen_skill_ids(session.id),
        )

    def _complete(self, session: MatchSession, result: MatchResult) -> None:
        if {item.skill_id for item in result.recommendations} & self.users.get_seen_skill_ids(session.id):
            raise InvalidRecommendations("Already reviewed recommendation IDs.")
        self.users.finish_match_session(session.id, "completed", result.recommendations)

    async def _run(
        self, session: MatchSession, profile: dict[str, Any], feedback: list[dict[str, Any]],
    ) -> None:
        stage = "retrieval"
        matcher = self.matchers[session.engine]
        engine_name = "Codex" if session.engine == "codex" else "Claude Code"
        try:
            # Retrieval and source enrichment share the matcher's overall time budget.
            async with asyncio.timeout(getattr(matcher, "timeout", 180)):
                candidates = await run_sync(self._search, session)
                stage = "enrichment"
                enriched = await self.enricher.enrich(candidates[:40])
                request = bounded_request(session.task, profile, feedback, enriched)
                # An empty candidate set is a successful no-fit result, with no CLI invocation.
                result = MatchResult(recommendations=[])
                if request["eligible_skills"]:
                    stage = "matcher"
                    raw = await matcher.match(request)
                    stage = "validation"
                    try:
                        result = MatchResult.model_validate_json(raw, strict=True)
                    except ValidationError as exc:
                        raise InvalidRecommendations from exc
            # The output schema cannot enforce membership in this request's candidate set.
            ids = [item.skill_id for item in result.recommendations]
            eligible = {skill["id"] for skill in request["eligible_skills"]}
            if len(ids) != len(set(ids)) or not set(ids).issubset(eligible):
                raise InvalidRecommendations("Invalid recommendation IDs.")
            stage = "persistence"
            await run_sync(self._complete, session, result)
        except asyncio.CancelledError:
            # Cancel and shutdown own the terminal status; do not overwrite it as a failure here.
            raise
        except TimeoutError:
            await self._fail(session.id, "Matching timed out. Try again.")
        except MatchError as exc:
            await self._fail(session.id, str(exc))
        except InvalidRecommendations:
            await self._fail(session.id, f"{engine_name} returned invalid recommendations. Try again.")
        except Exception as exc:  # noqa: BLE001
            self._diagnose(session.id, stage, exc)
            await self._fail(session.id, "Matching failed unexpectedly. Try again.")

    async def _fail(self, session_id: str, message: str) -> None:
        try:
            await run_sync(self.users.finish_match_session, session_id, "failed", error=message)
        except Exception as exc:  # noqa: BLE001
            self._diagnose(session_id, "failure-persistence", exc)

    @staticmethod
    def _diagnose(session_id: str, stage: str, exc: Exception) -> None:
        if isinstance(exc, ExceptionGroup):
            for failure in exc.exceptions:
                MatchService._diagnose(session_id, stage, failure)
            return
        # Trace locations are useful; exception text, source lines and locals may
        # contain task text, provider stderr, credentials, or SQL parameters.
        locations = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__)
        )
        logger.error(
            "Matching failure session=%s stage=%s exception=%s locations=%s",
            session_id, stage, type(exc).__name__, locations,
        )
