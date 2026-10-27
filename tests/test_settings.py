from pathlib import Path

import pytest
from pydantic import ValidationError

from skinder.settings import Settings, load_settings


def test_environment_overrides_dotenv_without_rewriting_it(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    content = 'SKINDER_MAX_CONCURRENT_JOBS=2\nLETSUR_MODEL=from-file\nSKINDER_SOURCE_CONCURRENCY=1\n'
    path.write_text(content)
    monkeypatch.setenv('SKINDER_MAX_CONCURRENT_JOBS', '7')
    settings = Settings(_env_file=path)
    assert settings.max_concurrent_jobs == 7
    assert settings.letsur_model == 'from-file'
    assert settings.source_concurrency == 1
    assert path.read_text() == content


def test_credentials_are_required_only_by_their_entrypoint():
    settings = Settings(_env_file=None, database_url='postgresql://example/db')
    settings.require('ingest')
    settings.require('migrate')
    with pytest.raises(ValueError, match='GITHUB_CLIENT_ID'):
        settings.require('web')
    with pytest.raises(ValueError, match='LETSUR_API_KEY'):
        settings.require('worker')


@pytest.mark.parametrize('values', [
    {'max_concurrent_jobs': 0}, {'daily_budget_units': '-1'},
    {'job_heartbeat_seconds': 30, 'job_lease_seconds': 20},
    {'env': 'production', 'app_origin': 'http://example.com'},
    {'app_origin': 'https://example.com/private'},
    {'sync_min_interval_seconds': 1}, {'database_url': 'sqlite:///old.sqlite3'},
])
def test_invalid_operational_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_invalid_secret_is_not_in_startup_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DATABASE_URL', 'SYNTHETIC_SECRET_SENTINEL')
    with pytest.raises(SystemExit) as caught:
        load_settings('migrate')
    assert 'SYNTHETIC_SECRET_SENTINEL' not in str(caught.value)
    assert 'DATABASE_URL' in str(caught.value) or 'configuration' in str(caught.value)


def test_example_is_loadable_without_private_credentials():
    settings = Settings(_env_file=Path(__file__).parents[1] / '.env.example')
    settings.require('ingest')
    assert settings.daily_budget_units == 5
    assert settings.letsur_api_key.get_secret_value() == ''


def test_keyword_queries_are_loaded_from_json_and_normalized(monkeypatch):
    monkeypatch.setenv('SKINDER_SYNC_KEYWORDS', '["coding", " testing ", "coding"]')
    settings = Settings(_env_file=None)
    assert settings.sync_keywords == ['coding', 'testing']
    assert settings.sync_max_requests == 12 and settings.sync_daily_requests == 48
    assert settings.sync_min_interval_seconds == 6


@pytest.mark.parametrize('keywords', [[], [''], ['*'], [' coding* '], ['!!!']])
def test_invalid_keyword_scopes_are_rejected(keywords):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sync_keywords=keywords)


def test_keyword_queries_respect_provider_length_limit():
    assert Settings(_env_file=None, sync_keywords=['x' * 200]).sync_keywords == ['x' * 200]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sync_keywords=['x' * 201])
