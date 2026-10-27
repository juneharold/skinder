from hashlib import sha256
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from starlette.middleware.sessions import SessionMiddleware

from skinder.auth import SESSION_COOKIE, AuthService, GitHubIdentity, create_auth_router
from skinder.models import User
from skinder.orm import LoginSessionRecord, UserRecord


@pytest.fixture
def auth(db, settings):
    return AuthService(db, settings)


@pytest.fixture
def client(auth, settings):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret.get_secret_value(),
        session_cookie="skinder_oauth", max_age=600, same_site="lax",
        https_only=settings.secure_cookies)
    app.include_router(create_auth_router(auth))

    @app.get("/private")
    def private(user: Annotated[User, Depends(auth.require_user)]):
        return {"user_id": user.id}

    @app.post("/private")
    def mutate(user: Annotated[User, Depends(auth.require_csrf)]):
        return {"user_id": user.id}

    with TestClient(app, base_url=settings.app_origin, follow_redirects=False) as client:
        yield client


def mock_github(auth, *, identity=None, token_status=200, user_status=200):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(token_status, json={"access_token": "secret-provider-token",
                "token_type": "bearer", "scope": "read:user"})
        assert request.url.host == "api.github.com" and request.url.path == "/user"
        return httpx.Response(user_status, json=identity or {
            "id": 123, "login": "alice", "avatar_url": "https://avatars.githubusercontent.com/u/123",
        })

    auth.github.client_kwargs["transport"] = httpx.MockTransport(handle)
    return calls


def login(client):
    response = client.get("/api/auth/github")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query, client.get("/api/auth/github/callback", params={"state": query["state"][0], "code": "test-code"})


def test_oauth_state_pkce_and_opaque_login_session(auth, client, db, settings):
    calls = mock_github(auth)
    query, response = login(client)
    assert response.status_code == 303
    assert query["scope"] == ["read:user"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [settings.app_origin + "/api/auth/github/callback"]
    exchange = parse_qs(calls[0].content.decode())
    assert exchange["client_id"] == [settings.github_client_id]
    verifier = exchange["code_verifier"][0]
    import base64
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == query["code_challenge"][0]
    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie and "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert "secret-provider-token" not in str(client.cookies)
    state = client.get("/api/auth/me").json()
    assert state["user"]["login"] == "alice" and state["csrf_token"]
    assert client.get("/private").json() == {"user_id": state["user"]["id"]}
    with db.db_sessions() as session:
        stored = session.scalar(select(LoginSessionRecord))
        assert stored.id == sha256(cookie.encode()).hexdigest()
        assert stored.id != cookie
        assert stored.csrf_token == state["csrf_token"]
        assert session.scalar(select(UserRecord.github_id)) == 123


@pytest.mark.parametrize("state", [None, "wrong-state"])
def test_callback_rejects_missing_or_incorrect_state_before_http(auth, client, db, state):
    calls = mock_github(auth)
    client.get("/api/auth/github")
    params = {"code": "sensitive-code"}
    if state:
        params["state"] = state
    response = client.get("/api/auth/github/callback", params=params)
    assert response.status_code == 400
    assert calls == []
    assert client.get("/api/auth/me").json()["user"] is None
    assert "sensitive-code" not in response.text
    with db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(LoginSessionRecord)) == 0


def test_callback_rejects_expired_flow(auth, client, monkeypatch):
    calls = mock_github(auth)
    response = client.get("/api/auth/github")
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    import skinder.auth as module
    now = module.time()
    monkeypatch.setattr(module, "time", lambda: now + 601)
    assert client.get("/api/auth/github/callback", params={"state": state, "code": "code"}).status_code == 400
    assert calls == []


@pytest.mark.parametrize("failure", ["denied", "token", "user", "identity"])
def test_oauth_failures_do_not_create_session(auth, client, failure):
    mock_github(auth, token_status=500 if failure == "token" else 200,
        user_status=403 if failure == "user" else 200,
        identity={"id": "invalid", "login": "alice"} if failure == "identity" else None)
    response = client.get("/api/auth/github")
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    params = {"state": state, "code": "sensitive-code"}
    if failure == "denied":
        params.update(error="access_denied", error_description="sensitive-provider-details")
    response = client.get("/api/auth/github/callback", params=params)
    assert response.status_code == 400
    assert "sensitive" not in response.text and "secret-provider-token" not in response.text
    assert client.get("/api/auth/me").json()["user"] is None


def test_session_expiry_uses_database_time(auth, client, db):
    cookie = auth.create_session(GitHubIdentity(id=123, login="alice"))
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/private").status_code == 200
    with db.db_sessions.begin() as session:
        session.execute(update(LoginSessionRecord).values(expires_at=func.clock_timestamp()))
    assert client.get("/private").status_code == 401
    assert client.get("/api/auth/me").json() == {"user": None, "csrf_token": None}


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "https://evil.example", "X-CSRF-Token": "valid"},
    {"Origin": "http://127.0.0.1:8742", "X-CSRF-Token": "wrong"},
    {"Origin": "http://127.0.0.1:8742", "X-CSRF-Token": "valid", "Sec-Fetch-Site": "cross-site"},
])
def test_cross_site_and_missing_csrf_requests_cannot_logout(auth, client, headers):
    cookie = auth.create_session(GitHubIdentity(id=123, login="alice"))
    client.cookies.set(SESSION_COOKIE, cookie)
    state = client.get("/api/auth/me").json()
    headers = dict(headers)
    if headers.get("X-CSRF-Token") == "valid":
        headers["X-CSRF-Token"] = state["csrf_token"]
    assert client.post("/api/auth/logout", headers=headers).status_code == 403
    assert client.post("/private", headers=headers).status_code == 403
    assert auth.get_state(cookie).user is not None


def test_logout_revokes_copied_cookie_and_login_rotation_revokes_previous(auth, client, settings):
    cookie = auth.create_session(GitHubIdentity(id=123, login="alice"))
    replacement = auth.create_session(GitHubIdentity(id=123, login="renamed"), cookie)
    assert auth.get_state(cookie).user is None
    assert auth.get_state(replacement).user.login == "renamed"
    client.cookies.set(SESSION_COOKIE, replacement)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    headers = {"Origin": settings.app_origin, "X-CSRF-Token": csrf}
    assert client.post("/private", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert auth.get_state(replacement).user is None
    assert client.get("/private").status_code == 401


def test_secure_cookie_on_https(db, settings):
    settings = settings.model_copy(update={"app_origin": "https://skinder.example"})
    auth = AuthService(db, settings)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret.get_secret_value(),
        session_cookie="skinder_oauth", max_age=600, https_only=True)
    app.include_router(create_auth_router(auth))
    mock_github(auth)
    with TestClient(app, base_url=settings.app_origin, follow_redirects=False) as client:
        _, response = login(client)
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/auth/me").json()["user"]["login"] == "alice"
