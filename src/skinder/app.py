import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .matcher import CodexMatcher, MatchError
from .models import (
    OK,
    Catalog,
    Error,
    Feedback,
    FeedbackInput,
    Health,
    MatchInput,
    MatchResult,
    MatchSession,
    Profile,
    Saved,
    Skill,
)
from .store import Store

CATEGORIES = [
    "Development",
    "Design",
    "Writing",
    "Research",
    "Documents",
    "Data",
    "Productivity",
    "Marketing",
]
ROOT = Path(__file__).resolve().parents[2]


def load_catalog(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    skills = [Skill.model_validate(item) for item in data["skills"]]
    catalog = {skill.id: skill for skill in skills}
    if len(catalog) != len(skills):
        raise ValueError("Catalog has duplicate skill IDs.")
    return catalog


def create_app(*, db_path=None, catalog_path=None, matcher=None, frontend_path=None):
    db_path = Path(
        db_path or os.environ.get("SKINDER_DB_PATH", str(ROOT / ".data" / "skinder.sqlite3"))
    )
    catalog_path = Path(catalog_path or Path(__file__).parent / "data" / "catalog.json")
    frontend_path = Path(frontend_path or ROOT / "frontend" / "dist")
    matcher = matcher or CodexMatcher()
    catalog = load_catalog(catalog_path)
    active_task = None
    active_session = None
    store = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal store
        store = Store(db_path)
        app.state.store = store
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

    async def run_match(session, request):
        try:
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
        return Health(catalog_count=len(catalog), codex=await matcher.health())

    @app.get("/api/catalog", response_model=Catalog)
    async def get_catalog():
        return Catalog(skills=list(catalog.values()), categories=CATEGORIES)

    @app.get("/api/skills/{skill_id}", response_model=Skill)
    async def get_skill(skill_id: str):
        if skill_id not in catalog:
            raise HTTPException(404, "Skill not found.")
        return catalog[skill_id]

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
        seen = store.seen(value.session_id) if value.session_id else set()
        eligible = [
            skill
            for skill in catalog.values()
            if value.agent in skill.compatibility and skill.id not in seen
        ]
        profile = store.profile().model_dump()
        profile["agent"] = value.agent
        fields = {
            "id",
            "name",
            "description",
            "category",
            "requirements",
            "tags",
            "workflow",
            "output_style",
            "evidence",
        }
        request = {
            "task": value.task,
            "profile": profile,
            "recent_feedback": [
                {**event, "skill": catalog[event["skill_id"]].model_dump(include=fields - {"id"})}
                for event in store.preference_events(value.agent)
                if event["skill_id"] in catalog
            ],
            "eligible_skills": [skill.model_dump(include=fields) for skill in eligible],
        }
        session = store.start(value.task, value.agent, value.session_id)
        active_session = session.id
        if eligible:
            active_task = asyncio.create_task(run_match(session, request))
        else:
            store.finish(session.id, "completed", [])
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

    @app.post("/api/feedback", response_model=Feedback)
    async def add_feedback(value: FeedbackInput):
        get_session(value.session_id)
        try:
            return store.feedback(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/feedback/{feedback_id}", response_model=OK)
    async def undo_feedback(feedback_id: str):
        store.undo(feedback_id)
        return OK()

    @app.get("/api/saved", response_model=Saved)
    async def get_saved():
        return Saved(
            items=[
                {"skill": catalog[row["skill_id"]], "saved_at": row["saved_at"]}
                for row in store.saved()
                if row["skill_id"] in catalog
            ]
        )

    @app.delete("/api/saved/{skill_id}", response_model=OK)
    async def delete_saved(skill_id: str):
        store.unsave(skill_id)
        return OK()

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
