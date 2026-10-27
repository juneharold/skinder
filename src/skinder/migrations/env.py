from alembic import context
from sqlalchemy import text

from skinder.database import Database
from skinder.orm import Base
from skinder.settings import load_settings


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      version_table_schema=context.config.attributes.get("schema"),
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    database = Database(load_settings("migrate"))
    try:
        with database.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(73519001)"))
            run(connection)
    finally:
        database.close()
