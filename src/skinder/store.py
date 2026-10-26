import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Feedback, FeedbackInput, MatchSession, Profile


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, agent TEXT NOT NULL,
                status TEXT NOT NULL, recommendations TEXT NOT NULL DEFAULT '[]',
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_skills (
                session_id TEXT REFERENCES sessions(id), skill_id TEXT NOT NULL,
                PRIMARY KEY(session_id, skill_id)
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), skill_id TEXT NOT NULL,
                action TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
                UNIQUE(session_id, skill_id)
            );
            CREATE TABLE IF NOT EXISTS bookmarks (
                skill_id TEXT PRIMARY KEY, saved_at TEXT NOT NULL, feedback_id TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute(
                "UPDATE sessions SET status='failed', error=?, updated_at=? "
                "WHERE status IN ('running', 'pending')",
                ("Matching was interrupted by a server restart. Try again when ready.", now()),
            )

    def close(self):
        self.db.close()

    def profile(self):
        row = self.db.execute("SELECT data FROM profile WHERE id=1").fetchone()
        return Profile.model_validate_json(row[0]) if row else Profile()

    def set_profile(self, profile):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO profile VALUES (1, ?)", (profile.model_dump_json(),)
            )
        return profile

    def session(self, session_id):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["recommendations"] = json.loads(data["recommendations"])
        data["feedback"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM feedback WHERE session_id=? ORDER BY created_at", (session_id,)
            )
        ]
        return MatchSession.model_validate(data)

    def current(self):
        row = self.db.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self.session(row[0]) if row else None

    def start(self, task, agent, session_id=None):
        timestamp = now()
        with self.db:
            if session_id:
                self.db.execute(
                    "UPDATE sessions SET status='running', error=NULL, updated_at=? WHERE id=?",
                    (timestamp, session_id),
                )
            else:
                session_id = str(uuid4())
                self.db.execute(
                    "INSERT INTO sessions (id,task,agent,status,created_at,updated_at) "
                    "VALUES (?,?,?,'running',?,?)",
                    (session_id, task, agent, timestamp, timestamp),
                )
        return self.session(session_id)

    def finish(self, session_id, status, recommendations=None, error=None):
        # Cancellation is authoritative even if a subprocess produces a late result.
        if self.session(session_id).status != "running":
            return
        with self.db:
            self.db.execute(
                "UPDATE sessions SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, now(), session_id),
            )
            if recommendations is not None:
                self.db.execute(
                    "UPDATE sessions SET recommendations=? WHERE id=?",
                    (
                        json.dumps([r.model_dump() for r in recommendations]),
                        session_id,
                    ),
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO session_skills VALUES (?,?)",
                    [(session_id, r.skill_id) for r in recommendations],
                )

    def seen(self, session_id):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT skill_id FROM feedback WHERE session_id=?", (session_id,)
            )
        }

    def feedback(self, value: FeedbackInput):
        known = self.db.execute(
            "SELECT 1 FROM session_skills WHERE session_id=? AND skill_id=?",
            (value.session_id, value.skill_id),
        ).fetchone()
        if not known:
            raise ValueError("Feedback requires a skill recommended in this task session.")
        previous = self.db.execute(
            "SELECT * FROM feedback WHERE session_id=? AND skill_id=?",
            (value.session_id, value.skill_id),
        ).fetchone()
        if previous:
            if previous["action"] == value.action and previous["reason"] == value.reason:
                return Feedback.model_validate(dict(previous))
            raise ValueError("Undo the existing feedback before changing it.")
        result = Feedback(**value.model_dump(), id=str(uuid4()), created_at=now())
        with self.db:
            self.db.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?,?)",
                (
                    result.id,
                    result.session_id,
                    result.skill_id,
                    result.action,
                    result.reason,
                    result.created_at,
                ),
            )
            if value.action == "save":
                self.db.execute(
                    "INSERT OR IGNORE INTO bookmarks VALUES (?,?,?)",
                    (value.skill_id, result.created_at, result.id),
                )
        return result

    def undo(self, feedback_id):
        with self.db:
            row = self.db.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
            if not row:
                return
            self.db.execute("DELETE FROM feedback WHERE id=?", (feedback_id,))
            bookmark = self.db.execute(
                "SELECT * FROM bookmarks WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
            if bookmark:
                other = self.db.execute(
                    "SELECT id FROM feedback WHERE skill_id=? AND action='save' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["skill_id"],),
                ).fetchone()
                if other:
                    self.db.execute(
                        "UPDATE bookmarks SET feedback_id=? WHERE skill_id=?",
                        (other[0], row["skill_id"]),
                    )
                else:
                    self.db.execute("DELETE FROM bookmarks WHERE feedback_id=?", (feedback_id,))

    def saved(self):
        return self.db.execute("SELECT * FROM bookmarks ORDER BY saved_at DESC").fetchall()

    def unsave(self, skill_id):
        with self.db:
            self.db.execute("DELETE FROM bookmarks WHERE skill_id=?", (skill_id,))

    def preference_events(self, agent):
        # A plain pass and wrong-task reason are local exclusions, never global taste signals.
        rows = self.db.execute(
            "SELECT f.skill_id, f.action, f.reason, s.task, s.agent FROM feedback f "
            "JOIN sessions s ON s.id=f.session_id WHERE s.agent=? AND "
            "(f.action='save' OR f.reason IN ('wrong-style','too-much-setup')) "
            "ORDER BY f.created_at DESC LIMIT 30",
            (agent,),
        )
        return [dict(row) for row in rows]
