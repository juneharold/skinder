import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from skinder.database import create_database, migration_config
from skinder.orm import (
    BookmarkRecord,
    CatalogAliasRecord,
    CatalogBase,
    CatalogSkillRecord,
    FeedbackRecord,
    MatchSessionRecord,
    MatchSessionSkillRecord,
    ProfileRecord,
    RetrievalMemberRecord,
    RetrievalMetaRecord,
    SkillEmbeddingRecord,
    SourceCacheRecord,
    SyncPartitionRecord,
    SyncRunRecord,
    UserBase,
)


@pytest.mark.parametrize("kind,base", [("user", UserBase), ("catalog", CatalogBase)])
def test_fresh_database_matches_models_and_alembic_head(tmp_path, kind, base):
    engine = create_database(tmp_path / f"{kind}.sqlite3", kind)
    try:
        tables = set(inspect(engine).get_table_names())
        application_tables = {
            name for name in tables if name != "alembic_version" and not name.startswith("skills_fts")
        }
        assert application_tables == set(base.metadata.tables)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                ScriptDirectory.from_config(migration_config(kind)).get_current_head()
            )
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30_000
            config = migration_config(kind)
            config.attributes["connection"] = connection
            command.check(config)
    finally:
        engine.dispose()


def test_user_reopen_preserves_records_and_foreign_keys_rollback(tmp_path):
    path = tmp_path / "user.sqlite3"
    engine = create_database(path, "user")
    with Session(engine) as session, session.begin():
        session.add(ProfileRecord(id=1, data='{"language":"ko"}'))
        session.add(MatchSessionRecord(
            id="task", task="Find a skill", agent="codex", status="completed",
            created_at="2026-10-27", updated_at="2026-10-27",
        ))
        session.flush()
        session.add(MatchSessionSkillRecord(session_id="task", skill_id="skill"))
        session.add(FeedbackRecord(
            id="feedback", session_id="task", skill_id="skill", action="save",
            created_at="2026-10-27",
        ))
        session.add(BookmarkRecord(skill_id="skill", saved_at="2026-10-27", feedback_id=None))
    engine.dispose()

    engine = create_database(path, "user")
    try:
        with Session(engine) as session:
            assert session.get(ProfileRecord, 1).data == '{"language":"ko"}'
            assert session.get(MatchSessionRecord, "task").recommendations == "[]"
            assert session.get(MatchSessionRecord, "task").engine == "codex"
            assert session.get(FeedbackRecord, "feedback").action == "save"
            assert session.get(BookmarkRecord, "skill").feedback_id is None
        with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
            session.add(BookmarkRecord(skill_id="rolled-back", saved_at="2026-10-27"))
            session.flush()
            session.add(MatchSessionSkillRecord(session_id="missing-session", skill_id="skill"))
        with Session(engine) as session:
            assert session.get(BookmarkRecord, "rolled-back") is None
            assert session.get(MatchSessionSkillRecord, ("task", "skill")) is not None
    finally:
        engine.dispose()


def catalog_record():
    return CatalogSkillRecord(
        id="skill", data='{"name":"Example"}', provenance="bundled", name="Example",
        category="Writing", retrieval_text="Example", text_hash="first",
    )


def test_catalog_reopen_preserves_cache_sync_and_vector_identity(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    engine = create_database(path, "catalog")
    with Session(engine) as session, session.begin():
        identity = session.get(RetrievalMetaRecord, "identity").value
        session.add(catalog_record())
        session.add(CatalogAliasRecord(provider="skillsmp", external_id="external", skill_id="skill"))
        session.add(SourceCacheRecord(
            skill_id="skill", source_url="https://example.com/SKILL.md", fetched_at=123.5,
            content="# Instructions",
        ))
        session.add(SyncRunRecord(id=1, data='{"status":"paused"}'))
        session.add(SyncPartitionRecord(key="partition", status="pending", data='{"page":3}'))
        session.add(SkillEmbeddingRecord(
            vector_id=42, skill_id="skill", text_hash="first", model_fingerprint="model",
            vector=b"\x00\x01\x02\x03",
        ))
        session.add(RetrievalMemberRecord(generation="generation", vector_id=42))
        session.execute(text(
            "INSERT INTO skills_fts VALUES ('skill', 'Example', '', '', 'Hwang')"
        ))
    engine.dispose()

    engine = create_database(path, "catalog")
    try:
        with Session(engine) as session:
            assert session.get(RetrievalMetaRecord, "identity").value == identity
            assert session.get(RetrievalMetaRecord, "catalog_revision").value == "1"
            assert session.get(CatalogSkillRecord, "skill").data == '{"name":"Example"}'
            assert session.get(CatalogAliasRecord, ("skillsmp", "external")).skill_id == "skill"
            assert session.get(SourceCacheRecord, "skill").content == "# Instructions"
            assert session.get(SyncRunRecord, 1).data == '{"status":"paused"}'
            assert session.get(SyncPartitionRecord, "partition").data == '{"page":3}'
            assert session.get(SkillEmbeddingRecord, 42).vector == b"\x00\x01\x02\x03"
            assert session.get(RetrievalMemberRecord, ("generation", 42)) is not None
            assert session.execute(text(
                "SELECT skill_id FROM skills_fts WHERE skills_fts MATCH 'Hwang'"
            )).scalar_one() == "skill"
        with Session(engine) as session, session.begin():
            embedding = SkillEmbeddingRecord(
                skill_id="another", text_hash="second", model_fingerprint="model", vector=b"new",
            )
            session.add(embedding)
            session.flush()
            assert embedding.vector_id > 42
    finally:
        engine.dispose()


def test_catalog_revision_tracks_retrieval_changes(tmp_path):
    engine = create_database(tmp_path / "catalog.sqlite3", "catalog")

    def revision():
        with Session(engine) as session:
            return session.scalar(select(RetrievalMetaRecord.value).where(
                RetrievalMetaRecord.key == "catalog_revision"
            ))

    try:
        assert revision() == "0"
        with Session(engine) as session, session.begin():
            session.add(catalog_record())
        assert revision() == "1"
        with Session(engine) as session, session.begin():
            session.get(CatalogSkillRecord, "skill").name = "New display name"
        assert revision() == "1"
        with Session(engine) as session, session.begin():
            session.get(CatalogSkillRecord, "skill").text_hash = "second"
        assert revision() == "2"
        with Session(engine) as session, session.begin():
            session.delete(session.get(CatalogSkillRecord, "skill"))
        assert revision() == "3"
    finally:
        engine.dispose()


def test_failed_migration_rolls_back_schema_and_version(tmp_path):
    path = tmp_path / "conflicting.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE bookmarks (skill_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO bookmarks VALUES ('existing')")
    with pytest.raises(OperationalError):
        create_database(path, "user")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("bookmarks",),
        ]
        assert connection.execute("SELECT skill_id FROM bookmarks").fetchall() == [("existing",)]


@pytest.mark.parametrize("kind,section,variable", [
    ("user", "user", "SKINDER_USER_DB_PATH"),
    ("catalog", "catalog", "SKINDER_CATALOG_DB_PATH"),
])
def test_named_alembic_configuration_upgrades_and_downgrades(tmp_path, monkeypatch, kind, section,
                                                          variable):
    path = tmp_path / f"{kind}.sqlite3"
    monkeypatch.setenv(variable, str(path))
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), ini_section=section)
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "base")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == [("alembic_version",)]
    command.upgrade(config, "head")
    command.check(config)


def test_engine_migration_preserves_old_sessions_recommendations_and_saved_records(tmp_path):
    from skinder.user import UserStore

    path = tmp_path / "old-user.sqlite3"
    engine = create_database(path, "user")
    try:
        with engine.begin() as connection:
            config = migration_config("user")
            config.attributes["connection"] = connection
            command.downgrade(config, "0001_user")
            connection.exec_driver_sql(
                "INSERT INTO match_sessions (id, task, agent, status, recommendations, created_at, updated_at) "
                "VALUES ('old', 'Review changes', 'claude-code', 'completed', ?, 'created', 'updated')",
                ('[{"skill_id":"review","task_fit":"Task","taste_fit":"Style","caveat":null}]',),
            )
            connection.exec_driver_sql("INSERT INTO match_session_skills VALUES ('old', 'review')")
            connection.exec_driver_sql(
                "INSERT INTO feedback VALUES ('choice', 'old', 'review', 'save', NULL, 'created')"
            )
            connection.exec_driver_sql("INSERT INTO bookmarks VALUES ('review', 'created', 'choice')")
    finally:
        engine.dispose()
    store = UserStore(path)
    try:
        restored = store.get_match_session("old")
        assert restored.engine == "codex" and restored.agent == "claude-code"
        assert restored.recommendations[0].skill_id == "review"
        assert restored.feedback[0].id == "choice"
        assert store.get_bookmarks()[0]["skill_id"] == "review"
        assert store.get_bookmarks()[0]["feedback_id"] == "choice"
        with store.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO match_sessions (id, task, agent, status, created_at, updated_at) "
                "VALUES ('default', 'Review again', 'codex', 'completed', 'created', 'updated')"
            )
        assert store.get_match_session("default").engine == "codex"
        assert store.start_match_session("Another review", "codex", engine="claude-code").engine == "claude-code"
    finally:
        store.close()
