from sqlalchemy import inspect, text

from skinder.database import migrate


def test_migrations_are_repeatable_and_create_postgres_search(db):
    migrate(db)
    db.check_schema()
    with db.engine.connect() as connection:
        assert connection.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
        tables = set(inspect(connection).get_table_names(schema=db.schema))
        assert {'users', 'match_jobs', 'catalog_skills', 'skill_embeddings', 'daily_usage'} <= tables
        index = connection.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname=:schema AND indexname='one_active_job_per_user'"), {'schema': db.schema}).scalar()
        assert 'UNIQUE INDEX' in index and 'WHERE' in index
