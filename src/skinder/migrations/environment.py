import os
from pathlib import Path

from alembic import context
from sqlalchemy import URL, make_url

from skinder.database import sqlite_engine
from skinder.orm import CatalogBase, UserBase


def run_migrations(kind):
    config = context.config
    metadata = UserBase.metadata if kind == "user" else CatalogBase.metadata

    def include_name(name, type_, _parent_names):
        # SQLite owns the virtual table and its shadow tables; migrations manage FTS explicitly.
        return not (kind == "catalog" and type_ == "table" and (
            name == "skills_fts" or name.startswith("skills_fts_")
        ))

    def migrate(connection):
        context.configure(
            connection=connection, target_metadata=metadata,
            include_name=include_name, render_as_batch=True, transactional_ddl=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connection = config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return

    variable = "SKINDER_USER_DB_PATH" if kind == "user" else "SKINDER_CATALOG_DB_PATH"
    path = os.environ.get(variable)
    url = URL.create("sqlite", database=path) if path else make_url(
        config.get_main_option("sqlalchemy.url")
    )
    if context.is_offline_mode():
        context.configure(
            url=url, target_metadata=metadata, literal_binds=True,
            dialect_opts={"paramstyle": "named"}, transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = sqlite_engine(url)
    try:
        with (
            engine.connect().execution_options(sqlite_begin_mode="IMMEDIATE") as connection,
            connection.begin(),
        ):
            migrate(connection)
    finally:
        engine.dispose()
