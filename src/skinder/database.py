"""Connection pools and explicit migrations. Opening a store never changes schema."""
import re

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import sessionmaker

from .settings import Settings, load_settings

MIGRATION_LOCK = 73519001
CATALOG_MAINTENANCE_LOCK = 73519002
JOB_ADMISSION_LOCK = 73519003

class Database:
    def __init__(self, settings: Settings, *, schema: str | None = None):
        settings.require("migrate")
        url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql+psycopg")
        args = {"connect_timeout": settings.db_connect_timeout_seconds}
        if schema is not None:
            if not re.fullmatch(r"test_[a-f0-9]+", schema):
                raise ValueError("Only isolated test schemas may override search_path")
            args["options"] = f"-csearch_path={schema},public"
        self.schema = schema
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout_seconds,
            connect_args=args, hide_parameters=True)
        self.db_sessions = sessionmaker(self.engine, expire_on_commit=False)

    def check_schema(self) -> None:
        with self.engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if version != "0001_hosted":
                raise RuntimeError("Database schema is not current. Run skinder-migrate.")

    def close(self) -> None:
        self.engine.dispose()


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", "skinder:migrations")
    return config


def migrate(database: Database) -> None:
    config = migration_config()
    with database.engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
        config.attributes["connection"] = connection
        config.attributes["schema"] = database.schema
        command.upgrade(config, "head")


def main() -> None:
    database = Database(load_settings("migrate"))
    try:
        migrate(database)
    finally:
        database.close()
