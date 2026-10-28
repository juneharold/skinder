"""HTTP contracts use real account sessions and PostgreSQL; only inference is controlled."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_catalog_store import example_skill

from skinder.app import create_app
from skinder.auth import SESSION_COOKIE, GitHubIdentity
from skinder.models import MatchResult, Recommendation
from skinder.orm import MatchJobRecord, UserDailyUsageRecord
from skinder.provider import ProviderOutcome


@contextmanager
def signed_in(app, account=1001):
    auth = app.state.auth
    cookie = auth.create_session(GitHubIdentity(id=account, login=f'user-{account}'))
    with TestClient(app, base_url=app.state.settings.app_origin, follow_redirects=False) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        state = client.get('/api/auth/me').json()
        client.headers.update({'Origin': app.state.settings.app_origin,
                               'X-CSRF-Token': state['csrf_token']})
        yield client


@pytest.fixture
def backend(db, settings):
    app = create_app(settings=settings, database=db)
    for number in range(3):
        app.state.catalog.upsert(example_skill(number, description='Review Python code'))
    with signed_in(app) as client:
        yield SimpleNamespace(app=app, client=client, db=db, settings=settings)


def submit(client, *, key=None, session_id=None, task='Review Python code', agent='codex'):
    return client.post('/api/matches', json={'task': task, 'agent': agent,
        **({'session_id': session_id} if session_id else {})},
        headers={'Idempotency-Key': key or str(uuid4())})


def complete(app, skill_id='example-0'):
    job = app.state.jobs.claim('test-worker')
    recommendations = [] if skill_id is None else [Recommendation(
        skill_id=skill_id, task_fit='Relevant to Python review', taste_fit='Clear checks', caveat=None)]
    assert app.state.jobs.finish(job.id, job.lease_token,
        ProviderOutcome(result=MatchResult(recommendations=recommendations)))
    return job


def test_public_catalog_and_identity_but_personal_routes_require_login(db, settings):
    app = create_app(settings=settings, database=db)
    with TestClient(app, base_url=settings.app_origin) as client:
        assert client.get('/api/catalog').json() == {
            'skills': [], 'categories': [], 'total': 0, 'has_more': False}
        assert client.get('/api/auth/me').json() == {'user': None, 'csrf_token': None}
        for path in ('/api/profile', '/api/saved', '/api/matches/current'):
            assert client.get(path).status_code == 401
        assert submit(client).status_code == 401


def test_matching_is_durable_and_submission_retry_returns_same_job(backend):
    client, app = backend.client, backend.app
    first = submit(client, key='intent').json()
    assert first['status'] == 'queued'
    assert submit(client, key='intent').json()['latest_job']['id'] == first['latest_job']['id']
    assert submit(client, key='intent', task='Different task').status_code == 409
    assert submit(client).status_code == 409
    with backend.db.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(MatchJobRecord)) == 1
        assert session.scalar(select(UserDailyUsageRecord.accepted_jobs)) == 1
    complete(app)
    result = client.get('/api/matches/' + first['id']).json()
    assert result['status'] == 'completed'
    assert result['recommendations'][0]['skill_id'] == 'example-0'


def test_two_app_instances_share_state_without_interrupting_jobs(backend):
    first = submit(backend.client).json()
    active = backend.app.state.jobs.claim('worker')
    second_app = create_app(settings=backend.settings, database=backend.db)
    with signed_in(second_app) as other:
        restored = other.get('/api/matches/current').json()
        assert restored['id'] == first['id'] and restored['status'] == 'running'
        assert backend.app.state.jobs.heartbeat(active.id, active.lease_token)
    assert backend.client.get('/api/matches/current').json()['status'] == 'running'


def test_accounts_cannot_read_continue_cancel_or_change_other_feedback(backend):
    owner = submit(backend.client).json()
    complete(backend.app)
    event = backend.client.post('/api/feedback', json={
        'session_id': owner['id'], 'skill_id': 'example-0', 'action': 'save'}).json()['feedback']
    with signed_in(backend.app, 1002) as other:
        assert other.get('/api/matches/' + owner['id']).status_code == 404
        assert other.get('/api/matches/current').json() is None
        assert submit(other, session_id=owner['id']).status_code == 404
        assert other.post('/api/jobs/' + owner['latest_job']['id'] + '/cancel').status_code == 404
        assert other.delete('/api/feedback/' + event['id']).status_code == 404
        assert other.post('/api/feedback', json={
            'session_id': owner['id'], 'skill_id': 'example-0', 'action': 'pass'}).status_code == 404
        assert other.get('/api/saved').json()['items'] == []
        assert submit(other).status_code == 202
    assert [item['skill']['id'] for item in backend.client.get('/api/saved').json()['items']] == ['example-0']


def test_cancel_is_job_specific_and_preserves_previous_batch(backend):
    original = submit(backend.client).json()
    complete(backend.app)
    more = submit(backend.client, session_id=original['id']).json()
    delayed = backend.client.post('/api/jobs/' + original['latest_job']['id'] + '/cancel').json()
    assert delayed['latest_job']['id'] == more['latest_job']['id']
    assert delayed['status'] == 'queued'
    cancelled = backend.client.post('/api/jobs/' + more['latest_job']['id'] + '/cancel').json()
    assert cancelled['status'] == 'cancelled'
    assert cancelled['recommendations'][0]['skill_id'] == 'example-0'
    assert backend.app.state.jobs.claim('worker') is None


def test_feedback_retry_and_independent_bookmark_survive_undo(backend):
    original = submit(backend.client).json()
    complete(backend.app)
    event = {'session_id': original['id'], 'skill_id': 'example-0', 'action': 'save'}
    first = backend.client.post('/api/feedback', json=event).json()
    assert backend.client.post('/api/feedback', json=event).json()['feedback']['id'] == first['feedback']['id']
    assert backend.client.put('/api/saved/example-0').status_code == 200
    undone = backend.client.delete('/api/feedback/' + first['feedback']['id']).json()
    assert [item['skill']['id'] for item in undone['items']] == ['example-0']


def test_undo_retry_can_be_reconciled_from_owned_session_and_saved_collection(backend):
    original = submit(backend.client).json()
    complete(backend.app)
    event = backend.client.post('/api/feedback', json={
        'session_id': original['id'], 'skill_id': 'example-0', 'action': 'save'}).json()
    path = '/api/feedback/' + event['feedback']['id']

    # The deletion commits even if the client never receives this response.
    assert backend.client.delete(path).status_code == 200
    assert backend.client.delete(path).status_code == 404
    restored = backend.client.get('/api/matches/' + original['id']).json()
    assert restored['feedback'] == []
    assert [item['skill_id'] for item in restored['recommendations']] == ['example-0']
    assert backend.client.get('/api/saved').json()['items'] == []


def test_mutations_require_csrf_and_exact_origin(backend):
    client = backend.client
    assert client.put('/api/profile', json={'output_style': 'private'},
        headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.put('/api/profile', json={'output_style': 'private'},
        headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert client.get('/api/profile').json()['output_style'] == ''
    assert client.put('/api/profile', json={'output_style': 'plain'}).status_code == 200
    assert client.get('/api/profile').json()['output_style'] == 'plain'


def test_validation_omits_submitted_secret_and_foreign_host_is_rejected(backend):
    result = backend.client.post('/api/matches', json={'task': 'private-secret-value', 'agent': 'secret-invalid-agent'},
        headers={'Idempotency-Key': 'intent'})
    assert result.status_code == 422 and 'private-secret-value' not in result.text
    assert 'secret-invalid-agent' not in result.text
    assert backend.client.get('/api/catalog', headers={'Host': 'foreign.example'}).status_code == 403
    assert backend.client.get('/health/ready', headers={'Host': 'healthcheck.railway.app'}).status_code == 200


def test_daily_quota_is_shared_across_api_instances(db, settings):
    settings = settings.model_copy(update={'matches_per_user_per_day': 1})
    app = create_app(settings=settings, database=db)
    with signed_in(app) as client:
        first = submit(client, key='first').json()
        client.post('/api/jobs/' + first['latest_job']['id'] + '/cancel')
        assert submit(client, key='first').status_code == 202
    second = create_app(settings=settings, database=db)
    with signed_in(second) as client:
        assert submit(client).status_code == 429


def test_logout_revokes_session(backend):
    assert backend.client.post('/api/auth/logout').status_code == 204
    assert backend.client.get('/api/auth/me').json()['user'] is None
    assert backend.client.get('/api/profile').status_code == 401


def test_request_logs_omit_query_values_and_resource_identifiers(backend, caplog):
    import logging
    caplog.set_level(logging.INFO, logger='skinder.http')
    backend.client.get('/health/live?code=synthetic-oauth-secret')
    backend.client.get('/api/skills/synthetic-private-identifier')
    records = [record for record in caplog.records if record.name == 'skinder.http']
    assert [(record.route, record.status) for record in records] == [
        ('/health/live', 200), ('/api/skills/{skill_id}', 404)]
    from skinder.diagnostics import JsonFormatter
    output = '\n'.join(JsonFormatter().format(record) for record in records)
    assert 'synthetic-oauth-secret' not in output
    assert 'synthetic-private-identifier' not in output
