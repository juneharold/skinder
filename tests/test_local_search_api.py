import json

import pytest
from test_api import match
from test_catalog_store import example_skill

from skinder.catalog import CatalogStore
from skinder.retrieval import HybridRetriever
from skinder.sources import match_metadata


class MetadataOnly:
    async def enrich(self, skills):
        return [match_metadata(skill) for skill in skills]


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
