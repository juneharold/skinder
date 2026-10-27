"""GitHub login and opaque database sessions.

Mount create_auth_router(auth), and install Starlette SessionMiddleware with
settings.session_secret, session_cookie='skinder_oauth', max_age=600,
same_site='lax', https_only=settings.secure_cookies. The signed cookie holds only
short-lived OAuth state and the PKCE verifier. Protect personal reads with
Depends(auth.require_user) and writes with Depends(auth.require_csrf).
"""
from datetime import timedelta
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import time
from typing import Annotated

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from .database import Database
from .models import AuthState, User
from .orm import LoginSessionRecord, UserRecord
from .settings import Settings

SESSION_COOKIE = "skinder_session"


class GitHubIdentity(BaseModel):
    model_config = ConfigDict(strict=True)
    id: int = Field(gt=0)
    login: str = Field(min_length=1)
    avatar_url: str | None = None


def _cookie_hash(cookie: str) -> str:
    return sha256(cookie.encode()).hexdigest()


class AuthService:
    def __init__(self, database: Database, settings: Settings):
        self.db_sessions = database.db_sessions
        self.settings = settings
        oauth = OAuth()
        self.github = oauth.register("github",
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret.get_secret_value(),
            authorize_url="https://github.com/login/oauth/authorize",
            access_token_url="https://github.com/login/oauth/access_token",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user", "code_challenge_method": "S256",
                           "token_endpoint_auth_method": "client_secret_post",
                           "timeout": settings.oauth_http_timeout_seconds},
        )

    def create_session(self, identity: GitHubIdentity, previous_cookie: str | None = None) -> str:
        cookie = token_urlsafe(32)
        with self.db_sessions.begin() as db:
            user = db.scalar(insert(UserRecord).values(github_id=identity.id,
                login=identity.login, avatar_url=identity.avatar_url).on_conflict_do_update(
                index_elements=[UserRecord.github_id], set_={
                    "login": identity.login, "avatar_url": identity.avatar_url,
                }).returning(UserRecord))
            if previous_cookie:
                db.execute(delete(LoginSessionRecord).where(
                    LoginSessionRecord.id == _cookie_hash(previous_cookie)))
            db.add(LoginSessionRecord(id=_cookie_hash(cookie), user_id=user.id,
                csrf_token=token_urlsafe(32), expires_at=func.clock_timestamp()
                + timedelta(seconds=self.settings.session_ttl_seconds)))
        return cookie

    def get_state(self, cookie: str | None) -> AuthState:
        if not cookie:
            return AuthState()
        with self.db_sessions() as db:
            row = db.execute(select(LoginSessionRecord, UserRecord)
                .join(UserRecord, UserRecord.id == LoginSessionRecord.user_id).where(
                    LoginSessionRecord.id == _cookie_hash(cookie),
                    LoginSessionRecord.expires_at > func.clock_timestamp())).first()
            if row is None:
                return AuthState()
            login_session, user = row
            return AuthState(user=User.model_validate(user, from_attributes=True),
                             csrf_token=login_session.csrf_token)

    def require_user(self, request: Request) -> User:
        state = self.get_state(request.cookies.get(SESSION_COOKIE))
        if state.user is None:
            raise HTTPException(401, "Sign in to continue.")
        return state.user

    def require_csrf(self, request: Request) -> User:
        state = self.get_state(request.cookies.get(SESSION_COOKIE))
        if state.user is None:
            raise HTTPException(401, "Sign in to continue.")
        csrf = request.headers.get("x-csrf-token", "")
        if (request.headers.get("origin") != self.settings.app_origin
                or request.headers.get("sec-fetch-site") == "cross-site"
                or not compare_digest(csrf.encode(), state.csrf_token.encode())):
            raise HTTPException(403, "Request verification failed.")
        return state.user

    def logout(self, cookie: str | None) -> None:
        if cookie:
            with self.db_sessions.begin() as db:
                db.execute(delete(LoginSessionRecord).where(
                    LoginSessionRecord.id == _cookie_hash(cookie)))


def create_auth_router(auth: AuthService) -> APIRouter:
    router = APIRouter(prefix="/api/auth")

    @router.get("/github")
    async def github_login(request: Request):
        request.session.clear()
        response = await auth.github.authorize_redirect(request,
            auth.settings.app_origin + "/api/auth/callback")
        request.session["oauth_started_at"] = time()
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/callback")
    async def github_callback(request: Request):
        try:
            started_at = request.session.get("oauth_started_at", 0)
            if not 0 <= time() - started_at < 600:
                raise ValueError("Expired OAuth state")
            token = await auth.github.authorize_access_token(request)
            result = await auth.github.get("user", token=token,
                headers={"Accept": "application/vnd.github+json"})
            result.raise_for_status()
            identity = GitHubIdentity.model_validate(result.json())
        except (OAuthError, httpx.HTTPError, ValueError, KeyError, TypeError):
            # Provider responses can contain authorization codes and tokens.
            request.session.clear()
            raise HTTPException(400, "GitHub sign-in failed. Please try again.") from None
        request.session.clear()
        cookie = await run_in_threadpool(auth.create_session, identity,
                                        request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse(auth.settings.app_origin + "/", status_code=303)
        response.set_cookie(SESSION_COOKIE, cookie, max_age=auth.settings.session_ttl_seconds,
            httponly=True, secure=auth.settings.secure_cookies, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/me", response_model=AuthState)
    def me(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return auth.get_state(request.cookies.get(SESSION_COOKIE))

    @router.post("/logout", status_code=204)
    def logout(request: Request, user: Annotated[User, Depends(auth.require_csrf)]):
        auth.logout(request.cookies.get(SESSION_COOKIE))
        request.session.clear()
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=auth.settings.secure_cookies,
                               httponly=True, samesite="lax")
        return response

    return router
