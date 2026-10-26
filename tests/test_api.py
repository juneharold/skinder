import asyncio
import json
import os
import sqlite3

import pytest

from skinder.ownership import DatabaseOwnership
from skinder.user import UserStore


async def finish(client, session_id):
    for _ in range(500):
        response = await client.get(f"/api/matches/{session_id}")
        session = response.json()
        if session["status"] not in ("pending", "running"):
            return session
        await asyncio.sleep(0.01)
    pytest.fail("Matching did not finish")


async def match(client, **overrides):
    response = await client.post(
        "/api/matches",
        json={
            "task": "Review my Python changes",
            "agent": "codex",
            **overrides,
        },
    )
    assert response.status_code == 202, response.text
    return await finish(client, response.json()["id"])


async def feedback(client, session, skill_id="skill-0", action="save", reason=None):
    response = await client.post(
        "/api/feedback",
        json={
            "session_id": session["id"],
            "skill_id": skill_id,
            "action": action,
            "reason": reason,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["feedback"]


async def test_catalog_health_and_profile_persist_without_matching(backend):
    async with backend.client() as client:
        assert (await client.get("/api/matches/current")).json() is None
        assert len((await client.get("/api/catalog")).json()["skills"]) == 16
        assert (await client.get("/api/skills/skill-0")).json()["name"] == "Skill 0"
        assert (await client.get("/api/skills/missing")).status_code == 404
        health = (await client.get("/api/health")).json()
        assert health["engines"]["codex"]["authenticated"] is True
        profile = {
            "agent": "claude-code",
            "planning": "plan-first",
            "communication": "concise",
            "verification": "thorough",
            "output_style": "Give me a short table.",
        }
        assert (await client.put("/api/profile", json=profile)).json() == profile
    async with backend.client() as client:
        assert (await client.get("/api/profile")).json() == profile
        backend.configure(login_exit=1)
        assert (await client.get("/api/health")).json()["engines"]["codex"]["authenticated"] is False
        assert (await client.get("/api/saved")).json() == {"items": []}


async def test_match_feedback_bookmark_undo_and_restore(backend):
    async with backend.client() as client:
        session = await match(client)
        assert session["status"] == "completed"
        assert [r["skill_id"] for r in session["recommendations"]] == [
            "skill-0",
            "skill-1",
            "skill-2",
        ]
        saved = await feedback(client, session)
        assert await feedback(client, session) == saved
        assert len((await client.get("/api/saved")).json()["items"]) == 1
        current = (await client.get("/api/matches/current")).json()
        assert current["recommendations"] == session["recommendations"]
        assert len(current["feedback"]) == 1
    async with backend.client() as client:
        assert (await client.get("/api/matches/current")).json()["id"] == session["id"]
        assert (await client.get("/api/saved")).json()["items"][0]["skill"]["id"] == "skill-0"
        await client.delete(f"/api/feedback/{saved['id']}")
        assert (await client.get("/api/saved")).json() == {"items": []}
        assert (await client.get("/api/matches/current")).json()["feedback"] == []


async def test_undo_preserves_save_from_another_task(backend):
    async with backend.client() as client:
        first = await match(client)
        a = await feedback(client, first)
        second = await match(client, task="Review a different codebase")
        b = await feedback(client, second)
        await client.delete(f"/api/feedback/{a['id']}")
        assert len((await client.get("/api/saved")).json()["items"]) == 1
        await client.delete(f"/api/feedback/{b['id']}")
        assert (await client.get("/api/saved")).json()["items"] == []


async def test_removing_bookmark_keeps_feedback_and_undo_cannot_delete_new_save(backend):
    async with backend.client() as client:
        first = await match(client)
        a = await feedback(client, first)
        await client.delete("/api/saved/skill-0")
        assert len((await client.get(f"/api/matches/{first['id']}")).json()["feedback"]) == 1
        second = await match(client, task="Another review task")
        await feedback(client, second)
        await client.delete(f"/api/feedback/{a['id']}")
        assert len((await client.get("/api/saved")).json()["items"]) == 1


async def test_contextual_feedback_and_explicit_preferences_reach_next_batch(backend):
    async with backend.client() as client:
        first = await match(client, task="Write Python regression tests")
        await feedback(client, first, "skill-0", "pass")
        await feedback(client, first, "skill-1", "pass", "wrong-task")
        await feedback(client, first, "skill-2", "pass", "wrong-style")
        second = await match(client, task="Review a pull request")
        await feedback(client, second, "skill-0", "save")
        await feedback(client, second, "skill-1", "pass", "too-much-setup")
        profile = (await client.get("/api/profile")).json()
        profile.update(communication="detailed", output_style="Explain tradeoffs")
        await client.put("/api/profile", json=profile)
        continued = await match(client, task=first["task"], session_id=first["id"])
        request = json.loads((backend.root / "request.json").read_text())
        assert request["profile"]["communication"] == "detailed"
        assert request["profile"]["output_style"] == "Explain tradeoffs"
        assert {e["reason"] for e in request["recent_feedback"]} == {
            None,
            "wrong-style",
            "too-much-setup",
        }
        assert {e["task"] for e in request["recent_feedback"]} == {first["task"], second["task"]}
        assert not {"skill-0", "skill-1", "skill-2", "skill-15"} & {
            s["id"] for s in request["eligible_skills"]
        }
        assert len(continued["feedback"]) == 3
        assert all(
            r["skill_id"] not in {"skill-0", "skill-1", "skill-2"}
            for r in continued["recommendations"]
        )
        assert (await client.get(f"/api/matches/{second['id']}")).json()[
            "recommendations"
        ] == second["recommendations"]


def recommendation(skill_id):
    return {"skill_id": skill_id, "task_fit": "Task fit", "taste_fit": "Style fit", "caveat": None}


async def test_feedback_accepts_recommendations_from_an_earlier_batch(backend):
    async with backend.client() as client:
        first = await match(client)
        backend.configure(result={"recommendations": [recommendation("skill-3")]})
        continued = await match(client, session_id=first["id"])
        assert [item["skill_id"] for item in continued["recommendations"]] == ["skill-3"]
        decision = await feedback(client, continued, "skill-0")
        restored = (await client.get(f"/api/matches/{first['id']}")).json()
        assert restored["feedback"] == [decision]
        assert (await client.get("/api/saved")).json()["items"][0]["skill"]["id"] == "skill-0"


@pytest.mark.parametrize(
    "result",
    [
        "not json",
        {"recommendations": [recommendation("invented")]},
        {"recommendations": [recommendation("skill-15")]},
        {"recommendations": [recommendation("skill-0"), recommendation("skill-0")]},
        {"recommendations": [recommendation(f"skill-{i}") for i in range(13)]},
        {"recommendations": [{"skill_id": "skill-0"}]},
        {"recommendations": [], "unexpected": "field"},
    ],
)
@pytest.mark.parametrize("engine", ["codex", "claude-code"])
async def test_invalid_model_output_never_replaces_previous_batch(backend, result, engine):
    async with backend.client() as client:
        first = await match(client, engine=engine)
        backend.configure(engine=engine, result=result)
        failed = await match(client, engine=engine, session_id=first["id"])
        assert failed["status"] == "failed"
        assert failed["recommendations"] == first["recommendations"]
        assert ("Codex" if engine == "codex" else "Claude") in failed["error"]


async def test_reviewed_skill_rejected_and_legitimate_no_fit_committed(backend):
    async with backend.client() as client:
        first = await match(client)
        await feedback(client, first)
        backend.configure(result={"recommendations": [recommendation("skill-0")]})
        failed = await match(client, session_id=first["id"])
        assert failed["status"] == "failed"
        assert failed["recommendations"] == first["recommendations"]
        backend.configure(result={"recommendations": []})
        no_fit = await match(client, session_id=first["id"])
        assert no_fit["status"] == "completed"
        assert no_fit["recommendations"] == []
        assert len(no_fit["feedback"]) == 1


async def wait_for_process(backend, engine="codex"):
    filename = "pid" if engine == "codex" else "claude-pid"
    for _ in range(100):
        if (backend.root / filename).exists():
            return int((backend.root / filename).read_text())
        await asyncio.sleep(0.01)
    pytest.fail(f"Fake {engine} did not start")


@pytest.mark.parametrize("engine", ["codex", "claude-code"])
async def test_cancel_reaps_child_and_prevents_overlapping_jobs(backend, engine):
    backend.configure(engine=engine, sleep=30)
    async with backend.client() as client:
        response = await client.post(
            "/api/matches", json={"task": "Review changes", "agent": "codex", "engine": engine}
        )
        session_id = response.json()["id"]
        pid = await wait_for_process(backend, engine)
        other_engine = "claude-code" if engine == "codex" else "codex"
        assert (
            await client.post("/api/matches", json={
                "task": "Another task", "agent": "codex", "engine": other_engine,
            })
        ).status_code == 409
        cancelled = (await client.post(f"/api/matches/{session_id}/cancel")).json()
        assert cancelled["status"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await asyncio.sleep(0.02)
        assert (await client.get(f"/api/matches/{session_id}")).json()["status"] == "cancelled"
        backend.configure(engine=engine)
        assert (await match(client, engine=engine))["status"] == "completed"


@pytest.mark.parametrize("engine", ["codex", "claude-code"])
async def test_timeout_reaps_child_and_failed_process_does_not_leak_credentials(backend, engine):
    backend.configure(engine=engine, sleep=30)
    async with backend.client(timeout=1) as client:
        failed = await match(client, engine=engine)
        assert failed["status"] == "failed"
        assert "timed out" in failed["error"]
        with pytest.raises(ProcessLookupError):
            os.kill(int((backend.root / ("pid" if engine == "codex" else "claude-pid")).read_text()), 0)
        backend.configure(engine=engine, exit=1)
        failed = await match(client, engine=engine)
        assert failed["status"] == "failed"
        assert "secret-that-must-not-leak" not in json.dumps(failed)


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_restart_marks_inflight_failed_without_repeating(backend, status):
    async with backend.client() as client:
        first = await match(client)
    with sqlite3.connect(backend.user_db_path) as db:
        db.execute("UPDATE match_sessions SET status=? WHERE id=?", (status, first["id"]))
    (backend.root / "request.json").unlink()
    async with backend.client() as client:
        restored = (await client.get("/api/matches/current")).json()
        assert restored["status"] == "failed"
        assert restored["recommendations"] == first["recommendations"]
        assert not (backend.root / "request.json").exists()


async def test_validation_unknown_feedback_and_mismatched_continuation(backend):
    async with backend.client() as client:
        for task in ("  ", "ab", "x" * 4001):
            response = await client.post("/api/matches", json={"task": task, "agent": "codex"})
            assert response.status_code == 422
            assert isinstance(response.json()["detail"], str)
        first = await match(client)
        assert (
            await client.post(
                "/api/matches",
                json={
                    "task": "Different task",
                    "agent": "codex",
                    "session_id": first["id"],
                },
            )
        ).status_code == 409
        assert (
            await client.post(
                "/api/feedback",
                json={
                    "session_id": first["id"],
                    "skill_id": "skill-8",
                    "action": "save",
                },
            )
        ).status_code == 409
        await feedback(client, first)
        assert (
            await client.post(
                "/api/feedback",
                json={
                    "session_id": first["id"],
                    "skill_id": "skill-0",
                    "action": "pass",
                },
            )
        ).status_code == 409
        profile = (await client.get("/api/profile")).json()
        profile["output_style"] = "a" * 2001
        assert (await client.put("/api/profile", json=profile)).status_code == 422


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"host": "evil.example:8742"},
        {"sec-fetch-site": "cross-site"},
        {"origin": "http://localhost:8742"},
    ],
)
async def test_cross_origin_calls_rejected(backend, headers):
    async with backend.client() as client:
        response = await client.post(
            "/api/matches", headers=headers, json={"task": "Review changes", "agent": "codex"}
        )
        assert response.status_code == 403
        assert (await client.get("/api/matches/current")).json() is None


async def test_localhost_origin_and_built_frontend(backend):
    frontend = backend.root / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>Skinder</html>")
    (frontend / "assets").mkdir()
    (frontend / "assets" / "app.js").write_text("console.log('Skinder')")
    async with backend.client(frontend_path=frontend) as client:
        assert (
            await client.get(
                "/api/catalog",
                headers={
                    "host": "localhost:8742",
                    "origin": "http://localhost:8742",
                },
            )
        ).status_code == 200
        assert (await client.get("/")).text == "<html>Skinder</html>"
        assert (await client.get("/saved")).status_code == 200
        assert (await client.get("/assets/app.js")).status_code == 200
        assert (await client.get("/api/unknown")).status_code == 404


async def test_recent_preference_history_is_bounded_and_agent_specific(backend):
    async with backend.client() as client:
        for number in range(11):
            session = await match(client, task=f"Review task number {number}")
            for skill in ("skill-0", "skill-1", "skill-2"):
                await feedback(client, session, skill)
        await match(client, task="Review the newest task")
        events = json.loads((backend.root / "request.json").read_text())["recent_feedback"]
        assert len(events) == 30
        assert all(event["task"] != "Review task number 0" for event in events)
        assert all(event["skill"]["workflow"] for event in events)
        await match(client, agent="claude-code")
        assert json.loads((backend.root / "request.json").read_text())["recent_feedback"] == []


@pytest.mark.parametrize("engine", ["codex", "claude-code"])
async def test_shutdown_reaps_running_child_and_keeps_interrupted_session(backend, engine):
    backend.configure(engine=engine, sleep=30)
    async with backend.client() as client:
        response = await client.post(
            "/api/matches", json={"task": "Review changes", "agent": "codex", "engine": engine}
        )
        session_id = response.json()["id"]
        pid = await wait_for_process(backend, engine)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    async with backend.client() as client:
        session = (await client.get(f"/api/matches/{session_id}")).json()
        assert session["status"] == "failed"
        assert "interrupted" in session["error"]


async def test_shutdown_persistence_failure_still_reaps_child_and_releases_database(
    backend, monkeypatch,
):
    def fail_write(*args, **kwargs):
        raise RuntimeError("Database write failed")

    backend.configure(sleep=30)
    with (
        monkeypatch.context() as patch,
        pytest.raises(RuntimeError, match="Database write failed"),
    ):
        async with backend.client() as client:
            response = await client.post(
                "/api/matches", json={"task": "Review changes", "agent": "codex"}
            )
            session_id = response.json()["id"]
            pid = await wait_for_process(backend)
            patch.setattr(UserStore, "finish_match_session", fail_write)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    owner = DatabaseOwnership(backend.user_db_path)
    owner.acquire()
    owner.release()
    async with backend.client() as client:
        session = (await client.get(f"/api/matches/{session_id}")).json()
        assert session["status"] == "failed"
        assert session["recommendations"] == []


async def test_missing_codex_leaves_browsing_and_profile_usable(backend, monkeypatch):
    monkeypatch.setattr("skinder.matcher.shutil.which", lambda name: None)
    async with backend.client() as client:
        health = (await client.get("/api/health")).json()
        assert health["engines"]["codex"]["installed"] is False
        assert (await match(client))["status"] == "failed"
        assert len((await client.get("/api/catalog")).json()["skills"]) == 16
        assert (await client.get("/api/profile")).json()["agent"] == "codex"


async def test_codex_process_is_configured_without_tools_or_inherited_instructions(backend):
    async with backend.client() as client:
        assert (await match(client))["status"] == "completed"
    args = json.loads((backend.root / "args.json").read_text())
    overrides = dict(args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "-c")
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral"}.issubset(args)
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert overrides["approval_policy"] == '"never"'
    assert overrides["web_search"] == '"disabled"'
    assert overrides["project_doc_max_bytes"] == "0"
    assert overrides["features.skip_host_skill_discovery"] == "true"
    for feature in (
        "hooks",
        "plugins",
        "apps",
        "shell_tool",
        "unified_exec",
        "multi_agent",
        "browser_use",
        "computer_use",
        "code_mode",
        "skill_search",
    ):
        assert overrides[f"features.{feature}"] == "false"
    assert not os.path.exists(args[args.index("--cd") + 1])


async def test_undo_new_save_does_not_restore_explicitly_removed_bookmark(backend):
    async with backend.client() as client:
        first = await match(client)
        old_save = await feedback(client, first)
        await client.delete("/api/saved/skill-0")
        second = await match(client, task="Review another codebase")
        new_save = await feedback(client, second)
        await client.delete(f"/api/feedback/{new_save['id']}")
        assert (await client.get("/api/saved")).json()["items"] == []
        old_session = (await client.get(f"/api/matches/{first['id']}")).json()
        assert old_session["feedback"] == [old_save]
        await match(client, task="Review recent changes")
        events = json.loads((backend.root / "request.json").read_text())["recent_feedback"]
        assert any(event["task"] == first["task"] and event["action"] == "save" for event in events)


async def test_direct_saves_are_idempotent_persisted_and_not_preference_events(backend):
    async with backend.client() as client:
        assert (await client.put("/api/saved/missing")).status_code == 404
        saved = (await client.put("/api/saved/skill-0")).json()
        assert [item["skill"]["id"] for item in saved["items"]] == ["skill-0"]
        assert (await client.put("/api/saved/skill-0")).json() == saved
        assert (await client.get("/api/matches/current")).json() is None
    async with backend.client() as client:
        assert (await client.get("/api/saved")).json() == saved
        session = await match(client)
        assert session["feedback"] == []
        request = json.loads((backend.root / "request.json").read_text())
        assert request["recent_feedback"] == []
        assert (await client.delete("/api/saved/skill-0")).json() == {"items": []}
        assert (await client.delete("/api/saved/skill-0")).json() == {"items": []}
        recreated = (await client.put("/api/saved/skill-0")).json()
        assert recreated["items"][0]["saved_at"] > saved["items"][0]["saved_at"]


@pytest.mark.parametrize("direct_first", [True, False])
async def test_undo_preserves_independent_save_and_original_timestamp(backend, direct_first):
    async with backend.client() as client:
        session = await match(client)
        if direct_first:
            original = (await client.put("/api/saved/skill-0")).json()
        decision = await feedback(client, session)
        if not direct_first:
            original = (await client.get("/api/saved")).json()
        assert (await client.put("/api/saved/skill-0")).json() == original
        assert (await client.delete(f"/api/feedback/{decision['id']}")).json() == original
        assert (await client.get(f"/api/matches/{session['id']}")).json()["feedback"] == []


async def test_feedback_responses_include_current_saved_collection_even_on_retry(backend):
    async with backend.client() as client:
        independent = (await client.put("/api/saved/skill-4")).json()["items"][0]
        session = await match(client)
        value = {"session_id": session["id"], "skill_id": "skill-0", "action": "save"}
        result = (await client.post("/api/feedback", json=value)).json()
        assert result["feedback"]["skill_id"] == "skill-0"
        assert result["saved"] == (await client.get("/api/saved")).json()
        assert {item["skill"]["id"] for item in result["saved"]["items"]} == {"skill-0", "skill-4"}
        remaining = {"items": [independent]}
        assert (await client.delete("/api/saved/skill-0")).json() == remaining
        retried = (await client.post("/api/feedback", json=value)).json()
        assert retried == {"feedback": result["feedback"], "saved": remaining}
        assert (
            await client.delete(f"/api/feedback/{result['feedback']['id']}")
        ).json() == remaining
        assert (await client.delete("/api/feedback/missing")).json() == remaining


async def test_old_feedback_cannot_remove_independent_resave(backend):
    async with backend.client() as client:
        session = await match(client)
        decision = await feedback(client, session)
        await client.delete("/api/saved/skill-0")
        independent = (await client.put("/api/saved/skill-0")).json()
        assert (await client.delete(f"/api/feedback/{decision['id']}")).json() == independent


async def test_removed_independent_save_does_not_protect_later_deck_save(backend):
    async with backend.client() as client:
        await client.put("/api/saved/skill-0")
        await client.delete("/api/saved/skill-0")
        session = await match(client)
        decision = await feedback(client, session)
        assert (await client.delete(f"/api/feedback/{decision['id']}")).json() == {"items": []}


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_matching_blocks_only_new_feedback_and_undo_for_that_session(backend, status):
    async with backend.client() as client:
        other = await match(client, task="Review another task")
        session = await match(client)
        decision = await feedback(client, session)
        await client.delete("/api/saved/skill-0")
        backend.configure(sleep=30)
        response = await client.post(
            "/api/matches",
            json={"task": session["task"], "agent": session["agent"], "session_id": session["id"]},
        )
        assert response.status_code == 202
        with sqlite3.connect(backend.user_db_path) as db:
            db.execute("UPDATE match_sessions SET status=? WHERE id=?", (status, session["id"]))
        value = {"session_id": session["id"], "skill_id": "skill-1", "action": "save"}
        assert (await client.post("/api/feedback", json=value)).status_code == 409
        assert (await client.delete(f"/api/feedback/{decision['id']}")).status_code == 409
        value["skill_id"] = "skill-0"
        retry = await client.post("/api/feedback", json=value)
        assert retry.status_code == 200
        assert retry.json() == {"feedback": decision, "saved": {"items": []}}
        assert (await client.delete("/api/feedback/missing")).json() == {"items": []}
        independent = (await client.put("/api/saved/skill-2")).json()
        assert [item["skill"]["id"] for item in independent["items"]] == ["skill-2"]
        assert (await client.delete("/api/saved/skill-2")).json() == {"items": []}
        other_decision = await feedback(client, other)
        assert (await client.delete(f"/api/feedback/{other_decision['id']}")).json() == {
            "items": []
        }
        current = (await client.get(f"/api/matches/{session['id']}")).json()
        assert current["feedback"] == [decision]
        with sqlite3.connect(backend.user_db_path) as db:
            db.execute("UPDATE match_sessions SET status='running' WHERE id=?", (session["id"],))
        await client.post(f"/api/matches/{session['id']}/cancel")
        assert (await client.delete(f"/api/feedback/{decision['id']}")).status_code == 200
