"""HTTP application. Matching executes in independent durable workers."""
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from .auth import AuthService, create_auth_router
from .catalog import CatalogStore
from .database import Database
from .jobs import JobError, JobStore
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
    User,
)
from .orm import FeedbackRecord, WorkerRecord
from .retrieval import HybridRetriever
from .settings import Settings, load_settings
from .user import UserStore
from .work import SyncWork, wait_for_work

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("skinder.http")


def create_app(*, settings: Settings | None = None, database: Database | None = None,
               frontend_path: str | Path | None = None) -> FastAPI:
    settings = settings or load_settings('web')
    settings.require('web')
    owns_database = database is None
    database = database or Database(settings)
    users = UserStore(database)
    catalog = CatalogStore(database, settings)
    jobs = JobStore(database, settings, users)
    auth = AuthService(database, settings)
    search = HybridRetriever(catalog, settings)
    work = SyncWork()
    frontend_path = Path(frontend_path or ROOT / 'frontend' / 'dist')

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await work.run(database.check_schema)
            yield
        finally:
            await wait_for_work(asyncio.create_task(work.drain()))
            if owns_database:
                database.close()

    app = FastAPI(title='Skinder API', version='0.2.0', lifespan=lifespan,
                  responses={code: {'model': Error} for code in (400, 401, 403, 404, 409, 422, 429)})
    app.state.auth = auth
    app.state.users = users
    app.state.catalog = catalog
    app.state.jobs = jobs
    app.state.database = database
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret.get_secret_value(),
                       session_cookie='skinder_oauth', max_age=600, same_site='lax',
                       https_only=settings.secure_cookies)
    app.include_router(create_auth_router(auth))

    @app.middleware('http')
    async def origin_boundary(request: Request, call_next):
        started = monotonic()
        # Infrastructure probes expose no account data and may use an internal Host header.
        probe = request.url.path in {'/health/live', '/health/ready'} and request.method == 'GET'
        if not probe and request.headers.get('host') != urlsplit(settings.app_origin).netloc:
            return JSONResponse({'detail': 'Unrecognized host.'}, 403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if ((origin is not None and origin != settings.app_origin)
                    or request.headers.get('sec-fetch-site') == 'cross-site'):
                return JSONResponse({'detail': 'Cross-origin writes are not allowed.'}, 403)
        response = await call_next(request)
        # Route patterns omit OAuth codes, search text, cookies, and resource identifiers.
        logger.info("http_request", extra={
            "method": request.method,
            "route": getattr(request.scope.get("route"), "path", "unmatched"),
            "status": response.status_code,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError):
        fields = ['.'.join(str(part) for part in error['loc'] if part != 'body')
                  for error in exc.errors()]
        return JSONResponse({'detail': 'Invalid request fields: ' + ', '.join(fields)}, 422)

    @app.exception_handler(JobError)
    async def job_error(_request: Request, exc: JobError):
        return JSONResponse({'detail': exc.detail}, exc.status_code)

    @app.get('/health/live', include_in_schema=False)
    async def live():
        return {'status': 'ok'}

    @app.get('/health/ready', include_in_schema=False)
    async def ready():
        try:
            await work.run(database.check_schema)
        except (SQLAlchemyError, RuntimeError):
            return JSONResponse({'status': 'unavailable'}, 503)
        return {'status': 'ok'}

    @app.get('/api/health', response_model=Health)
    async def health():
        def snapshot():
            with database.db_sessions() as session:
                cutoff = func.clock_timestamp() - timedelta(seconds=settings.job_lease_seconds)
                available = session.scalar(select(WorkerRecord.id).where(
                    WorkerRecord.ready.is_(True), WorkerRecord.last_seen > cutoff,
                    WorkerRecord.model == settings.letsur_model,
                ).limit(1)) is not None
            return Health(catalog_count=catalog.count(), model=settings.letsur_model,
                          matching_available=available, **search.status())
        return await work.run(snapshot)

    @app.get('/api/catalog', response_model=Catalog)
    async def get_catalog(q: str = Query('', max_length=4000), category: str | None = None,
                          offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        return await work.run(catalog.page, q=q, category=category, offset=offset, limit=limit)

    @app.get('/api/skills/{skill_id}', response_model=Skill)
    async def get_skill(skill_id: str):
        skill = await work.run(catalog.get, skill_id)
        if skill is None:
            raise HTTPException(404, 'Skill not found.')
        return skill

    @app.get('/api/profile', response_model=Profile)
    async def get_profile(user: Annotated[User, Depends(auth.require_user)]):
        return await work.run(users.get_profile, user.id)

    @app.put('/api/profile', response_model=Profile)
    async def put_profile(profile: Profile, user: Annotated[User, Depends(auth.require_csrf)]):
        return await work.run(users.set_profile, user.id, profile)

    @app.post('/api/matches', response_model=MatchSession, status_code=202)
    async def start_match(value: MatchInput, user: Annotated[User, Depends(auth.require_csrf)],
                          idempotency_key: str = Header(min_length=1, max_length=128)):
        return await work.run(jobs.submit, user.id, value, idempotency_key)

    @app.get('/api/matches/current', response_model=MatchSession | None)
    async def current_match(user: Annotated[User, Depends(auth.require_user)]):
        return await work.run(users.get_current_match_session, user.id)

    @app.get('/api/matches/{session_id}', response_model=MatchSession)
    async def get_match(session_id: UUID, user: Annotated[User, Depends(auth.require_user)]):
        result = await work.run(users.get_match_session, user.id, str(session_id))
        if result is None:
            raise HTTPException(404, 'Task session not found.')
        return result

    @app.post('/api/jobs/{job_id}/cancel', response_model=MatchSession)
    async def cancel_match(job_id: UUID, user: Annotated[User, Depends(auth.require_csrf)]):
        return await work.run(jobs.cancel, user.id, str(job_id))

    def saved_collection(user_id: str):
        return Saved(items=[{'skill': skill, 'saved_at': row['saved_at']}
                            for row in users.get_bookmarks(user_id)
                            if (skill := catalog.get(row['skill_id'])) is not None])

    @app.post('/api/feedback', response_model=FeedbackResult)
    async def add_feedback(value: FeedbackInput, user: Annotated[User, Depends(auth.require_csrf)]):
        def save():
            if users.get_match_session(user.id, value.session_id) is None:
                raise HTTPException(404, 'Task session not found.')
            try:
                feedback = users.add_feedback(user.id, value)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return FeedbackResult(feedback=feedback, saved=saved_collection(user.id))
        return await work.run(save)

    @app.delete('/api/feedback/{feedback_id}', response_model=Saved)
    async def undo_feedback(feedback_id: UUID, user: Annotated[User, Depends(auth.require_csrf)]):
        def undo():
            with database.db_sessions() as session:
                owned = session.scalar(select(FeedbackRecord.id).where(
                    FeedbackRecord.id == str(feedback_id), FeedbackRecord.user_id == user.id))
            if owned is None:
                raise HTTPException(404, 'Feedback not found.')
            try:
                users.undo_feedback(user.id, str(feedback_id))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return saved_collection(user.id)
        return await work.run(undo)

    @app.get('/api/saved', response_model=Saved)
    async def get_saved(user: Annotated[User, Depends(auth.require_user)]):
        return await work.run(saved_collection, user.id)

    @app.put('/api/saved/{skill_id}', response_model=Saved)
    async def put_saved(skill_id: str, user: Annotated[User, Depends(auth.require_csrf)]):
        def save():
            if catalog.get(skill_id) is None:
                raise HTTPException(404, 'Skill not found.')
            users.add_bookmark(user.id, skill_id)
            return saved_collection(user.id)
        return await work.run(save)

    @app.delete('/api/saved/{skill_id}', response_model=Saved)
    async def remove_saved(skill_id: str, user: Annotated[User, Depends(auth.require_csrf)]):
        def remove():
            users.remove_bookmark(user.id, skill_id)
            return saved_collection(user.id)
        return await work.run(remove)

    if (frontend_path / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=frontend_path / 'assets'), name='assets')

    @app.get('/{path:path}', include_in_schema=False)
    async def frontend(path: str):
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(404, 'API endpoint not found.')
        candidate = (frontend_path / path).resolve()
        if candidate.is_relative_to(frontend_path.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        if (frontend_path / 'index.html').is_file():
            return FileResponse(frontend_path / 'index.html')
        return JSONResponse({'detail': 'Build the frontend before starting Skinder.'}, 503)

    return app
