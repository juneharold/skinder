"""Shared catalog routes work without loading an embedding model or signing in."""
from fastapi.testclient import TestClient
from test_catalog_store import example_skill

from skinder.app import create_app


def test_catalog_pagination_and_search_across_app_instances(db, settings):
    first = create_app(settings=settings, database=db)
    for number in range(65):
        first.state.catalog.upsert(example_skill(number, description='PostgreSQL backups'))
    second = create_app(settings=settings, database=db)
    with TestClient(second, base_url=settings.app_origin) as client:
        page = client.get('/api/catalog', params={'q': 'PostgreSQL', 'limit': 50}).json()
        tail = client.get('/api/catalog', params={'q': 'PostgreSQL', 'offset': 50}).json()
        assert page['total'] == 65 and len(page['skills']) == 50 and page['has_more']
        assert len(tail['skills']) == 15 and not tail['has_more']
        assert not {item['id'] for item in page['skills']} & {item['id'] for item in tail['skills']}
        assert client.get('/api/skills/example-0').json()['id'] == 'example-0'
        health = client.get('/api/health').json()
        assert health['retrieval_mode'] == 'keyword-only'
        assert health['catalog_count'] == 65 and health['matching_available'] is False
        assert 'engines' not in health
