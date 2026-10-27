"""Every database test owns a disposable schema in an explicit test PostgreSQL server."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from skinder.database import Database, migrate
from skinder.orm import UserRecord
from skinder.settings import Settings


@pytest.fixture
def settings():
    return Settings(_env_file=None,
        database_url=os.environ.get('TEST_DATABASE_URL',
            'postgresql+psycopg://skinder:skinder@127.0.0.1:55432/skinder'),
        env='test', github_client_id='test-client', github_client_secret='test-secret',
        session_secret='test-session-secret-with-at-least-32-characters', letsur_api_key='test-key')


@pytest.fixture
def db(settings):
    schema = 'test_' + uuid4().hex
    admin = Database(settings)
    with admin.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    database = Database(settings, schema=schema)
    try:
        migrate(database)
        yield database
    finally:
        database.close()
        with admin.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.close()


@pytest.fixture
def database(db):
    return db


@pytest.fixture
def users(db):
    with db.db_sessions.begin() as session:
        first = UserRecord(github_id=1001, login='alice', avatar_url=None)
        second = UserRecord(github_id=1002, login='bob', avatar_url=None)
        session.add_all([first, second])
        session.flush()
    return first, second


@pytest.fixture
def catalog(db, settings):
    from skinder.catalog import CatalogStore
    return CatalogStore(db, settings)
