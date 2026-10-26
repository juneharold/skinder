import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .catalog import CatalogStore
from .matcher import CodexMatcher, MatchError
from .models import (
    Catalog,
    Error,
    FeedbackInput,
    FeedbackResult,
    Health,
    MatchInput,
    MatchResult,
    MatchSession,
    Profile,
    Saved,
    Skill,
)
from .retrieval import HybridRetriever
from .sources import SourceEnricher, bounded_request, match_metadata
from .store import Store
from .sync import sync_status

ROOT = Path(__file__).resolve().parents[2]


def create_app(
    *, db_path=None, catalog_db_path=None, matcher=None,
    frontend_path=None, retriever=None, enricher=None,
):
    db_path = Path(
        db_path or os.environ.get("SKINDER_DB_PATH", str(ROOT / ".data" / "skinder.sqlite3"))
    )
    catalog_db_path = Path(catalog_db_path or os.environ.get(
        "SKINDER_CATALOG_DB_PATH", db_path.with_name("catalog.sqlite3")
    ))
    frontend_path = Path(frontend_path or ROOT / "frontend" / "dist")
    matcher = matcher or CodexMatcher()
    catalog = None
    search = None
    sources = None
    active_task = None
    active_session = None
    store = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal store, catalog, search, sources
        store = Store(db_path)
        catalog = CatalogStore(catalog_db_path)
        search = retriever or HybridRetriever(catalog)
        sources = enricher or SourceEnricher(catalog)
        app.state.store = store
        app.state.catalog = catalog
        try:
            yield
        finally:
            if active_task is not None and not active_task.done():
                store.finish(
                    active_session, "failed", error="Matching was interrupted by shutdown."
                )
                active_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active_task
            store.close()
            catalog.close()

    app = FastAPI(
        title="Skinder API",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            400: {"model": Error},
            404: {"model": Error},
            409: {"model": Error},
            422: {"model": Error},
        },
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed_hosts = {"127.0.0.1:8742", "localhost:8742"}
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Use http://127.0.0.1:8742 to access Skinder."}, 403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            # The Vite proxy must rewrite Origin to the backend origin in development.
            if origin and origin != f"http://{host}":
                return JSONResponse({"detail": "Cross-origin API requests are not allowed."}, 403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site API requests are not allowed."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        issues = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"] if part != "body")
            issues.append(f"{location or 'request'}: {error['msg']}")
        return JSONResponse({"detail": "; ".join(issues)}, 422)

    def get_session(session_id):
        session = store.session(session_id)
        if session is None:
            raise HTTPException(404, "Task session not found.")
        return session

    async def run_match(session, profile, feedback):
        try:
            async with asyncio.timeout(getattr(matcher, "timeout", 180)):
                candidates = await asyncio.to_thread(
                    search.search, session.task, session.agent, store.seen(session.id)
                )
                enriched = await sources.enrich(candidates[:40])
                request = bounded_request(session.task, profile, feedback, enriched)
                if not request["eligible_skills"]:
                    store.finish(session.id, "completed", [])
                    return
                raw = await matcher.match(request)
                result = MatchResult.model_validate_json(raw, strict=True)
            ids = [recommendation.skill_id for recommendation in result.recommendations]
            eligible = {skill["id"] for skill in request["eligible_skills"]}
            if len(ids) != len(set(ids)) or not set(ids).issubset(eligible):
                raise ValueError("Invalid recommendation IDs.")
            # Feedback can arrive while a batch runs; do not return newly reviewed skills.
            if set(ids) & store.seen(session.id):
                raise ValueError("Already reviewed recommendation IDs.")
            store.finish(session.id, "completed", result.recommendations)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            store.finish(session.id, "failed", error="Matching timed out. Try again.")
        except MatchError as exc:
            store.finish(session.id, "failed", error=str(exc))
        except (ValidationError, ValueError):
            store.finish(
                session.id, "failed", error="Codex returned invalid recommendations. Try again."
            )
        except Exception:  # noqa: BLE001 - keep background failures visible without leaking process data
            store.finish(session.id, "failed", error="Matching failed unexpectedly. Try again.")

    @app.get("/api/health", response_model=Health)
    async def health():
        status = await asyncio.to_thread(search.status)
        return Health(
            catalog_count=catalog.count(), codex=await matcher.health(),
            sync_status=sync_status(catalog), **status,
        )

    @app.get("/api/catalog", response_model=Catalog)
    async def get_catalog(
        q: str = Query(default="", max_length=4000), category: str | None = None,
        offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
    ):
        return await asyncio.to_thread(catalog.page, q=q, category=category, offset=offset, limit=limit)

    @app.get("/api/skills/{skill_id}", response_model=Skill)
    async def get_skill(skill_id: str):
        skill = catalog.get(skill_id)
        if skill is None:
            raise HTTPException(404, "Skill not found.")
        return skill

    @app.get("/api/profile", response_model=Profile)
    async def get_profile():
        return store.profile()

    @app.put("/api/profile", response_model=Profile)
    async def put_profile(profile: Profile):
        return store.set_profile(profile)

    @app.post("/api/matches", response_model=MatchSession, status_code=202)
    async def start_match(value: MatchInput):
        nonlocal active_task, active_session
        if active_task is not None and not active_task.done():
            raise HTTPException(409, "A match is already running. Wait for it or cancel it first.")
        if value.session_id:
            previous = get_session(value.session_id)
            if previous.task != value.task or previous.agent != value.agent:
                raise HTTPException(409, "A continuation must use the same task and target agent.")
        profile = store.profile().model_dump()
        profile["agent"] = value.agent
        feedback = []
        for event in store.preference_events(value.agent):
            skill = catalog.get(event["skill_id"])
            if skill:
                feedback.append({**event, "skill": match_metadata(skill)})
        session = store.start(value.task, value.agent, value.session_id)
        active_session = session.id
        active_task = asyncio.create_task(run_match(session, profile, feedback))
        return store.session(session.id)

    @app.get("/api/matches/current", response_model=MatchSession | None)
    async def current_match():
        return store.current()

    @app.get("/api/matches/{session_id}", response_model=MatchSession)
    async def get_match(session_id: str):
        return get_session(session_id)

    @app.post("/api/matches/{session_id}/cancel", response_model=MatchSession)
    async def cancel_match(session_id: str):
        session = get_session(session_id)
        if session.status == "running":
            store.finish(session_id, "cancelled")
            if active_session == session_id and active_task is not None:
                active_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active_task
        return get_session(session_id)

    @app.post("/api/feedback", response_model=FeedbackResult)
    async def add_feedback(value: FeedbackInput):
        get_session(value.session_id)
        try:
            feedback = store.feedback(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return FeedbackResult(feedback=feedback, saved=await get_saved())

    @app.delete("/api/feedback/{feedback_id}", response_model=Saved)
    async def undo_feedback(feedback_id: str):
        try:
            store.undo(feedback_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await get_saved()

    @app.get("/api/saved", response_model=Saved)
    async def get_saved():
        return Saved(
            items=[
                {"skill": skill, "saved_at": row["saved_at"]}
                for row in store.saved()
                if (skill := catalog.get(row["skill_id"])) is not None
            ]
        )

    @app.put("/api/saved/{skill_id}", response_model=Saved)
    async def put_saved(skill_id: str):
        if catalog.get(skill_id) is None:
            raise HTTPException(404, "Skill not found.")
        store.save(skill_id)
        return await get_saved()

    @app.delete("/api/saved/{skill_id}", response_model=Saved)
    async def delete_saved(skill_id: str):
        store.unsave(skill_id)
        return await get_saved()

    if frontend_path.is_dir():
        assets = frontend_path / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found.")
        candidate = (frontend_path / path).resolve()
        if candidate.is_relative_to(frontend_path.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        index = frontend_path / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Build the frontend with npm run build in frontend/."}, 503)

    return app
