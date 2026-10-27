from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import update

from skinder.models import FeedbackInput, Profile, Recommendation
from skinder.orm import (
    MatchJobRecord,
    MatchSessionRecord,
    MatchSessionSkillRecord,
    UserRecord,
)
from skinder.user import UserStore


def completed_session(db, user, skills=("review",), agent="codex"):
    session_id, job_id = str(uuid4()), str(uuid4())
    with db.db_sessions.begin() as session:
        row = MatchSessionRecord(id=session_id, user_id=user.id, task="Review Python changes",
            agent=agent, latest_job_id=job_id, recommendations=[Recommendation(
                skill_id=skill, task_fit="Reviews code", taste_fit="Concise output", caveat=None,
            ).model_dump() for skill in skills])
        session.add(row)
        session.flush()
        session.add(MatchJobRecord(id=job_id, user_id=user.id, session_id=session_id,
            idempotency_key=str(uuid4()), request_hash="hash", input={}, model="test",
            prompt_version="1", status="completed"))
        session.add_all(MatchSessionSkillRecord(user_id=user.id, session_id=session_id,
            skill_id=skill) for skill in skills)
    return session_id, job_id


def save(store, user, session_id, skill="review"):
    return store.add_feedback(user.id, FeedbackInput(session_id=session_id, skill_id=skill, action="save"))


def test_personal_data_is_isolated(db, users):
    alice, bob = users
    store = UserStore(db)
    session_id, _ = completed_session(db, alice)
    store.set_profile(alice.id, Profile(communication="concise"))
    feedback = save(store, alice, session_id)
    assert store.get_profile(bob.id) == Profile()
    assert store.get_profile(alice.id).communication == "concise"
    assert store.get_match_session(bob.id, session_id) is None
    assert store.get_current_match_session(bob.id) is None
    assert store.get_seen_skill_ids(bob.id, session_id) == set()
    assert store.get_preference_events(bob.id, "codex") == []
    assert store.get_bookmarks(bob.id) == []
    with pytest.raises(ValueError):
        save(store, bob, session_id)
    store.undo_feedback(bob.id, feedback.id)
    store.remove_bookmark(bob.id, "review")
    assert store.get_match_session(alice.id, session_id).feedback == [feedback]
    assert len(store.get_bookmarks(alice.id)) == 1


def test_concurrent_feedback_retries_persist_one_choice_and_bookmark(db, users):
    user = users[0]
    store = UserStore(db)
    session_id, _ = completed_session(db, user)
    ready = Barrier(4)

    def retry():
        ready.wait(timeout=5)
        return save(store, user, session_id)

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: retry(), range(4)))
    assert len({result.id for result in results}) == 1
    assert store.get_match_session(user.id, session_id).feedback == [results[0]]
    assert len(store.get_bookmarks(user.id)) == 1
    store.undo_feedback(user.id, results[0].id)
    assert store.get_match_session(user.id, session_id).feedback == []
    assert store.get_bookmarks(user.id) == []
    with db.db_sessions() as session:
        assert session.get(UserRecord, user.id).mutation_seq == 2


def test_retry_during_active_continuation_does_not_restore_bookmark(db, users):
    user = users[0]
    store = UserStore(db)
    session_id, job_id = completed_session(db, user, ("review", "other"))
    feedback = save(store, user, session_id)
    store.remove_bookmark(user.id, "review")
    with db.db_sessions.begin() as session:
        session.execute(update(MatchJobRecord).where(MatchJobRecord.id == job_id).values(status="queued"))
    assert save(store, user, session_id) == feedback
    assert store.get_bookmarks(user.id) == []
    with pytest.raises(ValueError):
        save(store, user, session_id, "other")
    with pytest.raises(ValueError):
        store.undo_feedback(user.id, feedback.id)


def test_active_other_session_does_not_block_feedback(db, users):
    user = users[0]
    store = UserStore(db)
    first, _ = completed_session(db, user)
    _, job_id = completed_session(db, user)
    with db.db_sessions.begin() as session:
        session.execute(update(MatchJobRecord).where(MatchJobRecord.id == job_id).values(status="running"))
    feedback = save(store, user, first)
    store.undo_feedback(user.id, feedback.id)
    assert store.get_seen_skill_ids(user.id, first) == set()


def test_feedback_authorizes_prior_batches_but_rejects_unknown_skill(db, users):
    user = users[0]
    store = UserStore(db)
    session_id, _ = completed_session(db, user)
    with db.db_sessions.begin() as session:
        session.execute(update(MatchSessionRecord).where(MatchSessionRecord.id == session_id)
            .values(recommendations=[]))
    feedback = save(store, user, session_id)
    assert store.get_seen_skill_ids(user.id, session_id) == {"review"}
    assert store.get_match_session(user.id, session_id).feedback == [feedback]
    with pytest.raises(ValueError):
        save(store, user, session_id, "unknown")
    with pytest.raises(ValueError):
        store.add_feedback(user.id, FeedbackInput(session_id=session_id, skill_id="review", action="pass"))


def test_independent_bookmark_survives_concurrent_undo(db, users):
    user = users[0]
    store = UserStore(db)
    session_id, _ = completed_session(db, user)
    feedback = save(store, user, session_id)
    ready = Barrier(2)

    def undo():
        ready.wait(timeout=5)
        store.undo_feedback(user.id, feedback.id)

    def bookmark():
        ready.wait(timeout=5)
        store.add_bookmark(user.id, "review")

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(undo), workers.submit(bookmark)]
        for future in futures:
            future.result()
    assert store.get_match_session(user.id, session_id).feedback == []
    bookmarks = store.get_bookmarks(user.id)
    assert len(bookmarks) == 1 and bookmarks[0]["feedback_id"] is None


def test_undo_transfers_bookmark_only_within_current_generation(db, users):
    user = users[0]
    store = UserStore(db)
    sessions = [completed_session(db, user)[0] for _ in range(4)]
    old = save(store, user, sessions[0])
    store.remove_bookmark(user.id, "review")
    owner = save(store, user, sessions[1])
    later = save(store, user, sessions[2])
    store.undo_feedback(user.id, owner.id)
    assert store.get_bookmarks(user.id)[0]["feedback_id"] == later.id
    store.undo_feedback(user.id, later.id)
    assert store.get_bookmarks(user.id) == []
    assert store.get_match_session(user.id, sessions[0]).feedback == [old]


def test_preferences_only_include_agent_relevant_taste(db, users):
    user = users[0]
    store = UserStore(db)
    first, _ = completed_session(db, user, ("saved", "plain", "task", "style", "setup"))
    for skill, action, reason in [("saved", "save", None), ("plain", "pass", None),
            ("task", "pass", "wrong-task"), ("style", "pass", "wrong-style"),
            ("setup", "pass", "too-much-setup")]:
        store.add_feedback(user.id, FeedbackInput(session_id=first, skill_id=skill, action=action, reason=reason))
    second, _ = completed_session(db, user, ("claude",), agent="claude-code")
    save(store, user, second, "claude")
    assert [event["skill_id"] for event in store.get_preference_events(user.id, "codex")] == ["setup", "style", "saved"]
    assert store.get_current_match_session(user.id).id == second


def test_session_read_keeps_one_snapshot_when_feedback_and_job_change(db, users):
    from sqlalchemy import event

    user = users[0]
    store = UserStore(db)
    session_id, job_id = completed_session(db, user, ("review", "other"))
    first = save(store, user, session_id)
    changed = False

    def change_after_session_read(connection, cursor, statement, parameters, context, many):
        nonlocal changed
        if changed or not statement.startswith("SELECT match_sessions."):
            return
        changed = True
        save(store, user, session_id, "other")
        with db.db_sessions.begin() as writer:
            writer.execute(update(MatchJobRecord).where(MatchJobRecord.id == job_id)
                .values(status="failed", error="Later failure"))

    event.listen(db.engine, "after_cursor_execute", change_after_session_read)
    try:
        snapshot = store.get_match_session(user.id, session_id)
    finally:
        event.remove(db.engine, "after_cursor_execute", change_after_session_read)
    assert changed
    assert snapshot.status == "completed" and snapshot.error is None
    assert snapshot.feedback == [first]
    latest = store.get_match_session(user.id, session_id)
    assert latest.status == "failed" and len(latest.feedback) == 2
