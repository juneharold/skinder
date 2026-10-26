import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .catalog import CatalogStore
from .matcher import CodexMatcher
from .matching import Enricher, Matcher, MatchService, MatchServiceError, Retriever
from .models import (
    Catalog,
    Error,
    FeedbackInput,
    FeedbackResult,
    Health,
    MatchInput,
    MatchSession,
    Profile,
    Saved,
    Skill,
)
from .ownership import DatabaseOwnership
from .retrieval import HybridRetriever
from .sources import SourceEnricher
from .sync import sync_status
from .user import UserStore
from .work import SyncWork, run_sync, wait_for_work

ROOT = Path(__file__).resolve().parents[2]


def create_app(
    *, user_db_path: str | Path | None = None, catalog_db_path: str | Path | None = None,
    matcher: Matcher | None = None, frontend_path: str | Path | None = None,
    retriever: Retriever | None = None, enricher: Enricher | None = None,
) -> FastAPI:
    user_db_path = Path(
        user_db_path or os.environ.get("SKINDER_USER_DB_PATH", str(ROOT / ".data" / "user.sqlite3"))
    )
    catalog_db_path = Path(catalog_db_path or os.environ.get(
        "SKINDER_CATALOG_DB_PATH", user_db_path.with_name("catalog.sqlite3")
    ))
    frontend_path = Path(frontend_path or ROOT / "frontend" / "dist")
    matcher = matcher or CodexMatcher()
    user_store: UserStore
    catalog_store: CatalogStore
    search: Retriever
    service: MatchService
    work = SyncWork()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal user_store, catalog_store, search, service
        ownership = DatabaseOwnership(user_db_path)
        cleanup = ExitStack()
        # Callbacks run in reverse order: close the stores before releasing ownership.
        cleanup.callback(ownership.release)
        started = False

        def initialize() -> None:
            nonlocal user_store, catalog_store, search, service
            ownership.acquire()
            user_store = UserStore(user_db_path)
            cleanup.callback(user_store.close)
            # Recovery is safe only after excluding another server with live matching work.
            user_store.recover_interrupted_sessions()
            catalog_store = CatalogStore(catalog_db_path)
            cleanup.callback(catalog_store.close)
            search = retriever or HybridRetriever(catalog_store)
            sources = enricher or SourceEnricher(catalog_store)
            service = MatchService(user_store, catalog_store, matcher, search, sources)
            app.state.user_store = user_store
            app.state.catalog_store = catalog_store
            app.state.match_service = service

        async def shutdown() -> None:
            try:
                # Cancelled request workers may still be using the stores.
                await work.drain()
                if started:
                    await service.shutdown()
            finally:
                await run_sync(cleanup.close)

        try:
            await run_sync(initialize)
            started = True
            yield
        finally:
            await wait_for_work(asyncio.create_task(shutdown()))

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
    async def local_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = request.headers.get("host", "")
        # A local socket alone does not stop a browser from reaching it through another hostname.
        allowed_hosts = {"127.0.0.1:8742", "localhost:8742"}
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Use http://127.0.0.1:8742 to access Skinder."}, 403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            # The Vite proxy must rewrite Origin to the backend origin in development.
            if origin and origin != f"http://{host}":
                return JSONResponse({"detail": "Cross-origin API requests are not allowed."}, 403)
            # Browser requests can omit Origin; retain the separate cross-site check.
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site API requests are not allowed."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        # Validation details can include submitted values; expose only locations and messages.
        issues = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"] if part != "body")
            issues.append(f"{location or 'request'}: {error['msg']}")
        return JSONResponse({"detail": "; ".join(issues)}, 422)

    @app.exception_handler(MatchServiceError)
    async def match_service_error(request: Request, exc: MatchServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, exc.status_code)

    @app.get("/api/health", response_model=Health)
    async def health() -> Health:
        def snapshot() -> dict[str, Any]:
            return {"catalog_count": catalog_store.count(),
                    "sync_status": sync_status(catalog_store), **search.status()}

        status = await work.run(snapshot)
        return Health(codex=await matcher.health(), **status)

    @app.get("/api/catalog", response_model=Catalog)
    async def get_catalog(
        q: str = Query(default="", max_length=4000), category: str | None = None,
        offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
    ) -> Catalog:
        return await work.run(
            catalog_store.page, q=q, category=category, offset=offset, limit=limit
        )

    @app.get("/api/skills/{skill_id}", response_model=Skill)
    async def get_skill(skill_id: str) -> Skill:
        skill = await work.run(catalog_store.get, skill_id)
        if skill is None:
            raise HTTPException(404, "Skill not found.")
        return skill

    @app.get("/api/profile", response_model=Profile)
    async def get_profile() -> Profile:
        return await work.run(user_store.get_profile)

    @app.put("/api/profile", response_model=Profile)
    async def put_profile(profile: Profile) -> Profile:
        return await work.run(user_store.set_profile, profile)

    @app.post("/api/matches", response_model=MatchSession, status_code=202)
    async def start_match(value: MatchInput) -> MatchSession:
        return await service.start(value)

    @app.get("/api/matches/current", response_model=MatchSession | None)
    async def current_match() -> MatchSession | None:
        return await work.run(user_store.get_current_match_session)

    @app.get("/api/matches/{session_id}", response_model=MatchSession)
    async def get_match(session_id: str) -> MatchSession:
        return await work.run(service.get_session, session_id)

    @app.post("/api/matches/{session_id}/cancel", response_model=MatchSession)
    async def cancel_match(session_id: str) -> MatchSession:
        return await service.cancel(session_id)

    def saved_collection() -> Saved:
        # User data outlives catalog entries; omit missing skills without deleting their saves.
        return Saved(
            items=[
                {"skill": skill, "saved_at": row["saved_at"]}
                for row in user_store.get_bookmarks()
                if (skill := catalog_store.get(row["skill_id"])) is not None
            ]
        )

    @app.post("/api/feedback", response_model=FeedbackResult)
    async def add_feedback(value: FeedbackInput) -> FeedbackResult:
        def save_feedback() -> FeedbackResult:
            service.get_session(value.session_id)
            try:
                feedback = user_store.add_feedback(value)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            # Retries return the current collection, which may have changed since the original save.
            return FeedbackResult(feedback=feedback, saved=saved_collection())

        return await work.run(save_feedback)

    @app.delete("/api/feedback/{feedback_id}", response_model=Saved)
    async def undo_feedback(feedback_id: str) -> Saved:
        def undo() -> Saved:
            try:
                user_store.undo_feedback(feedback_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return saved_collection()

        return await work.run(undo)

    @app.get("/api/saved", response_model=Saved)
    async def get_saved() -> Saved:
        return await work.run(saved_collection)

    @app.put("/api/saved/{skill_id}", response_model=Saved)
    async def put_saved(skill_id: str) -> Saved:
        def save() -> Saved:
            if catalog_store.get(skill_id) is None:
                raise HTTPException(404, "Skill not found.")
            user_store.add_bookmark(skill_id)
            return saved_collection()

        return await work.run(save)

    @app.delete("/api/saved/{skill_id}", response_model=Saved)
    async def delete_saved(skill_id: str) -> Saved:
        def remove() -> Saved:
            user_store.remove_bookmark(skill_id)
            return saved_collection()

        return await work.run(remove)

    if frontend_path.is_dir():
        assets = frontend_path / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found.")
        # Resolve symlinks before checking containment so they cannot expose files outside dist.
        candidate = (frontend_path / path).resolve()
        if candidate.is_relative_to(frontend_path.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        index = frontend_path / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Build the frontend with npm run build in frontend/."}, 503)

    return app
