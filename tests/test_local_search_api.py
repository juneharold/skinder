import json
import sqlite3

import httpx
import pytest
from test_api import match
from test_catalog_store import example_skill

from skinder.app import create_app
from skinder.catalog import CatalogStore
from skinder.retrieval import HybridRetriever
from skinder.sources import match_metadata


class MetadataOnly:
    async def enrich(self, skills):
        return [match_metadata(skill) for skill in skills]


@pytest.mark.parametrize("user_source,catalog_source", [
    ("default", "default"),
    ("environment", "default"),
    ("environment", "environment"),
    ("explicit", "environment"),
    ("explicit", "explicit"),
])
async def test_database_paths_select_persisted_user_and_catalog_data(
    tmp_path, monkeypatch, user_source, catalog_source,
):
    monkeypatch.setattr("skinder.app.ROOT", tmp_path)
    monkeypatch.delenv("SKINDER_USER_DB_PATH", raising=False)
    monkeypatch.delenv("SKINDER_CATALOG_DB_PATH", raising=False)
    user_db_path = tmp_path / ".data" / "user.sqlite3"
    options = {}
    if user_source != "default":
        user_db_path = tmp_path / "environment" / "user.sqlite3"
        monkeypatch.setenv("SKINDER_USER_DB_PATH", str(user_db_path))
    if user_source == "explicit":
        user_db_path = tmp_path / "explicit" / "user.sqlite3"
        options["user_db_path"] = user_db_path
    catalog_db_path = user_db_path.with_name("catalog.sqlite3")
    if catalog_source != "default":
        catalog_db_path = tmp_path / "environment" / "catalog.sqlite3"
        monkeypatch.setenv("SKINDER_CATALOG_DB_PATH", str(catalog_db_path))
    if catalog_source == "explicit":
        catalog_db_path = tmp_path / "explicit" / "catalog.sqlite3"
        options["catalog_db_path"] = catalog_db_path

    catalog_store = CatalogStore(catalog_db_path)
    skill = example_skill(1)
    catalog_store.upsert(skill)
    catalog_store.close()
    app = create_app(**options)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742"
        ) as client,
    ):
        response = await client.put("/api/profile", json={"output_style": "Use a short table."})
        assert response.status_code == 200
        profile = response.json()
        catalog = (await client.get("/api/catalog")).json()
        assert [item["id"] for item in catalog["skills"]] == [skill.id]

    with sqlite3.connect(user_db_path) as connection:
        saved_profile = connection.execute("SELECT data FROM profile WHERE id=1").fetchone()[0]
        assert json.loads(saved_profile) == profile
    if user_source == "explicit":
        assert not (tmp_path / "environment" / "user.sqlite3").exists()


@pytest.mark.asyncio
async def test_fresh_catalog_can_be_browsed_and_matched_before_import(tmp_path, monkeypatch):
    monkeypatch.setattr("skinder.matcher.shutil.which", lambda name: None)
    app = create_app(
        user_db_path=tmp_path / "user.sqlite3", catalog_db_path=tmp_path / "catalog.sqlite3"
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742"
        ) as client,
    ):
        response = await client.get("/api/catalog")
        assert response.status_code == 200
        assert response.json() == {"skills": [], "categories": [], "total": 0, "has_more": False}
        session = await match(client)
        assert session["status"] == "completed"
        assert session["recommendations"] == []
        assert session["error"] is None


@pytest.mark.asyncio
async def test_persistent_imports_are_paged_saved_and_shortlisted_without_vector_setup(backend):
    catalog = CatalogStore(backend.root / "catalog.sqlite3")
    for number in range(85):
        catalog.upsert(example_skill(number, description="PostgreSQL disaster recovery", category="Databases"))
    catalog.upsert(example_skill(90, description="PostgreSQL disaster recovery", compatibility=["claude-code"]))
    retriever = HybridRetriever(catalog)
    async with backend.client(retriever=retriever, enricher=MetadataOnly()) as client:
        first = (await client.get("/api/catalog", params={"q": "PostgreSQL", "limit": 50})).json()
        second = (await client.get("/api/catalog", params={"q": "PostgreSQL", "offset": 50})).json()
        assert first["total"] == 86 and len(first["skills"]) == 50 and first["has_more"]
        assert len(second["skills"]) == 36 and not second["has_more"]
        ids = {skill["id"] for skill in first["skills"] + second["skills"]}
        assert len(ids) == 86
        detail = (await client.get("/api/skills/example-84")).json()
        assert detail["description"] == "PostgreSQL disaster recovery"
        assert detail["compatibility"] is None
        saved = (await client.put("/api/saved/example-84")).json()
        assert saved["items"][0]["skill"]["id"] == "example-84"
        session = await match(client, task="PostgreSQL disaster recovery")
        assert session["status"] == "completed" and session["recommendations"]
        request = json.loads((backend.root / "request.json").read_text())
        assert 1 <= len(request["eligible_skills"]) <= 40
        assert "example-90" not in {row["id"] for row in request["eligible_skills"]}
        assert all(row["description"] == "PostgreSQL disaster recovery" for row in request["eligible_skills"])
        health = (await client.get("/api/health")).json()
        assert health["catalog_count"] == 102
        assert health["retrieval_mode"] == "keyword-only"
        assert (await client.get("/api/catalog?limit=201")).status_code == 422
        assert (await client.get("/api/catalog?offset=-1")).status_code == 422
    async with backend.client(retriever=retriever, enricher=MetadataOnly()) as client:
        saved = (await client.get("/api/saved")).json()
        assert saved["items"][0]["skill"]["id"] == "example-84"
        assert (await client.get("/api/catalog", params={"q": "PostgreSQL"})).json()["total"] == 86
