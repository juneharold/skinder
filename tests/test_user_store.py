from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from skinder.models import FeedbackInput, Recommendation
from skinder.user import UserStore


def test_opening_another_store_does_not_interrupt_active_match(tmp_path):
    path = tmp_path / "user.sqlite3"
    first = UserStore(path)
    session = first.start_match_session("Review Python changes", "codex")
    second = UserStore(path)
    try:
        assert second.get_match_session(session.id).status == "running"
        assert first.get_match_session(session.id).error is None
        second.recover_interrupted_sessions()
        assert first.get_match_session(session.id).status == "failed"
    finally:
        second.close()
        first.close()


def test_concurrent_feedback_retries_persist_one_choice_and_bookmark(tmp_path):
    store = UserStore(tmp_path / "user.sqlite3")
    session = store.start_match_session("Review Python changes", "codex")
    store.finish_match_session(session.id, "completed", [Recommendation(
        skill_id="review", task_fit="Reviews code", taste_fit="Concise output", caveat=None,
    )])
    choice = FeedbackInput(session_id=session.id, skill_id="review", action="save")
    ready = Barrier(4)

    def save():
        ready.wait(timeout=5)
        return store.add_feedback(choice)

    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = [future.result() for future in [workers.submit(save) for _ in range(4)]]
        restored = store.get_match_session(session.id)
        assert len({result.id for result in results}) == 1
        assert restored.feedback == [results[0]]
        assert [bookmark["skill_id"] for bookmark in store.get_bookmarks()] == ["review"]
        store.undo_feedback(results[0].id)
        assert store.get_match_session(session.id).feedback == []
        assert store.get_bookmarks() == []
    finally:
        store.close()
