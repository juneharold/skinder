"""SQLite engine configuration and versioned database initialization."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event


def sqlite_engine(url):
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _record):
        # Explicit BEGIN also makes SQLite schema changes transactional on Python 3.11.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        mode = connection.get_execution_options().get("sqlite_begin_mode", "DEFERRED")
        if mode not in {"DEFERRED", "IMMEDIATE"}:
            raise ValueError("Unsupported SQLite transaction mode")
        connection.exec_driver_sql(f"BEGIN {mode}")

    return engine


def migration_config(kind):
    if kind not in {"user", "catalog"}:
        raise ValueError("Database kind must be 'user' or 'catalog'")
    config = Config()
    config.set_main_option("script_location", f"skinder:migrations/{kind}")
    return config


def create_database(path, kind):
    config = migration_config(kind)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sqlite_engine(URL.create("sqlite", database=str(path)))
    try:
        with (
            engine.connect().execution_options(sqlite_begin_mode="IMMEDIATE") as connection,
            connection.begin(),
        ):
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except BaseException:
        engine.dispose()
        raise
    return engine
