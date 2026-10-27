import json
import os

import pytest
from test_api import feedback, match

from skinder.models import MatchInput


@pytest.mark.parametrize("engine", ["codex", "claude-code"])
@pytest.mark.parametrize("agent", ["codex", "claude-code"])
async def test_engine_routes_independently_of_target_and_persists(backend, engine, agent):
    selected = "request.json" if engine == "codex" else "claude-request.json"
    other = "claude-request.json" if engine == "codex" else "request.json"
    async with backend.client() as client:
        session = await match(client, engine=engine, agent=agent)
        assert session["status"] == "completed"
        assert session["engine"] == engine and session["agent"] == agent
        request = json.loads((backend.root / selected).read_text())
        assert request["profile"]["agent"] == agent
        assert all(agent in skill["compatibility"] for skill in request["eligible_skills"])
        assert not (backend.root / other).exists()
    async with backend.client() as client:
        restored = (await client.get(f"/api/matches/{session['id']}")).json()
        assert restored == session
        assert not (backend.root / other).exists()


def test_engine_default_preserves_codex_and_rejects_unknown_engine():
    assert MatchInput(task="Review changes", agent="claude-code").engine == "codex"
    with pytest.raises(ValueError):
        MatchInput(task="Review changes", agent="codex", engine="unknown")


@pytest.mark.parametrize("engine", ["codex", "claude-code"])
async def test_continuation_requires_saved_engine_and_preserves_feedback(backend, engine):
    other = "claude-code" if engine == "codex" else "codex"
    async with backend.client() as client:
        session = await match(client, engine=engine)
        decision = await feedback(client, session)
        response = await client.post("/api/matches", json={
            "task": session["task"], "agent": session["agent"],
            "engine": other, "session_id": session["id"],
        })
        assert response.status_code == 409
        current = (await client.get(f"/api/matches/{session['id']}")).json()
        assert current["recommendations"] == session["recommendations"]
        assert current["feedback"] == [decision]
        continued = await match(client, engine=engine, session_id=session["id"])
        assert continued["engine"] == engine
        assert continued["status"] == "completed"
        assert continued["feedback"] == [decision]
        assert "skill-0" not in {item["skill_id"] for item in continued["recommendations"]}


@pytest.mark.parametrize("missing", ["codex", "claude"])
async def test_missing_engine_does_not_block_other_engine(backend, monkeypatch, missing):
    import shutil

    original = shutil.which
    monkeypatch.setattr("shutil.which", lambda name: None if name == missing else original(name))
    absent = "codex" if missing == "codex" else "claude-code"
    available = "claude-code" if absent == "codex" else "codex"
    async with backend.client() as client:
        health = (await client.get("/api/health")).json()["engines"]
        assert set(health) == {"codex", "claude-code"}
        assert health[absent]["installed"] is False and health[absent]["ready"] is False
        assert health[available]["ready"] is True
        assert (await match(client, engine=available))["status"] == "completed"
        assert (await match(client, engine=absent))["status"] == "failed"
        assert (await client.get("/api/catalog")).status_code == 200


@pytest.mark.parametrize("configuration,authenticated", [
    ({"auth": {"loggedIn": False}}, False),
    ({"login_exit": 1}, False),
    ({"auth_raw": "access_token=private-auth-output"}, False),
    ({"help": "--print --output-format"}, False),
])
async def test_claude_health_requires_login_and_safe_capabilities(backend, configuration, authenticated):
    backend.configure(engine="claude-code", **configuration)
    async with backend.client() as client:
        health = (await client.get("/api/health")).json()["engines"]
        assert health["claude-code"]["installed"] is True
        assert health["claude-code"]["authenticated"] is authenticated
        assert health["claude-code"]["ready"] is False
        assert health["codex"]["ready"] is True
        assert "private-auth-output" not in json.dumps(health)
        if "help" in configuration:
            failed = await match(client, engine="claude-code")
            assert failed["status"] == "failed"
            assert not (backend.root / "claude-request.json").exists()


@pytest.mark.parametrize("envelope", [
    None,
    [],
    {"type": "result", "subtype": "success", "is_error": False},
    {"type": "result", "subtype": "error", "is_error": False, "structured_output": {"recommendations": []}},
    {"type": "result", "subtype": "success", "is_error": True, "structured_output": {"recommendations": []}},
    {"type": "message", "subtype": "success", "is_error": False, "structured_output": {"recommendations": []}},
    {"type": "result", "subtype": "success", "is_error": False, "structured_output": "{\"recommendations\": []}"},
])
async def test_claude_rejects_unsuccessful_or_malformed_envelope(backend, envelope):
    async with backend.client() as client:
        first = await match(client, engine="claude-code")
        backend.configure(engine="claude-code", envelope=envelope)
        failed = await match(client, engine="claude-code", session_id=first["id"])
        assert failed["status"] == "failed"
        assert failed["recommendations"] == first["recommendations"]


@pytest.mark.parametrize("configuration", [
    {"raw": "private-output-not-json"},
    {"envelope": {"type": "result", "subtype": "success", "is_error": False,
                  "structured_output": {"recommendations": []},
                  "result": "private-output" * 100_000}},
    {"result": {"recommendations": [{"skill_id": "skill-0", "task_fit": "Task",
                                      "taste_fit": "Style", "caveat": "x" * 100_001}]}},
    {"result": {"recommendations": [{"skill_id": "skill-0", "task_fit": "Task",
                                      "taste_fit": "Style", "caveat": "가" * 40_000}]}},
])
async def test_claude_caps_output_and_does_not_expose_it(backend, configuration):
    backend.configure(engine="claude-code", **configuration)
    async with backend.client() as client:
        session = await match(client, engine="claude-code")
        assert session["status"] == "failed" and session["recommendations"] == []
        assert "private-output" not in json.dumps(session)
        assert "x" * 100 not in json.dumps(session)
        assert "가" * 100 not in json.dumps(session, ensure_ascii=False)


async def test_claude_process_has_no_tools_inherited_keys_or_persistent_session(backend, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_SESSION_ID", "CLAUDECODE", "CLAUDE_UNRECOGNIZED_OVERRIDE"):
        monkeypatch.setenv(name, "private-inherited-value")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(backend.root / "local-login"))
    async with backend.client() as client:
        assert (await client.get("/api/health")).json()["engines"]["claude-code"]["ready"] is True
        assert (await match(client, engine="claude-code", task="Private matching task"))["status"] == "completed"
    auth_args = json.loads((backend.root / "claude-auth-args.json").read_text())
    assert {"--safe-mode", "--setting-sources", "--settings", "auth", "status", "--json"}.issubset(auth_args)
    assert auth_args[auth_args.index("--setting-sources") + 1] == ""
    assert json.loads(auth_args[auth_args.index("--settings") + 1])["disableAllHooks"] is True
    args = json.loads((backend.root / "claude-args.json").read_text())
    assert "Private matching task" not in " ".join(args)
    assert {"--print", "--strict-mcp-config", "--no-session-persistence",
            "--disable-slash-commands", "--safe-mode", "--no-chrome"}.issubset(args)
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--settings") + 1])["disableAllHooks"] is True
    assert args[args.index("--output-format") + 1] == "json"
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert not {"--continue", "--resume", "--session-id", "--dangerously-skip-permissions"} & set(args)
    for filename in ("claude-env.json", "claude-check-env.json"):
        env = json.loads((backend.root / filename).read_text())
        assert env["CLAUDE_CONFIG_DIR"] == str(backend.root / "local-login")
        assert not [key for key in env if key.startswith(("ANTHROPIC_", "CLAUDE_"))
                    and key != "CLAUDE_CONFIG_DIR"]
        assert "CLAUDECODE" not in env
    assert not os.path.exists((backend.root / "claude-cwd").read_text())
