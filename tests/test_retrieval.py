import json
import math

import numpy as np
import pytest
from sqlalchemy import and_, func, select

from skinder import retrieval
from skinder.catalog import CatalogStore
from skinder.models import Skill
from skinder.orm import CatalogSkillRecord, SkillEmbeddingRecord
from skinder.retrieval import HybridRetriever, index_catalog


class TestEmbedder:
    """Known geometry lets the real vector index exercise retrieval deterministically."""

    __test__ = False

    def embed(self, texts):
        for text in texts:
            angle = 0 if text.startswith("query: ") or "nebula" in text else 1.5
            if "astronomy" in text:
                angle = 0.2
            vector = np.zeros(retrieval.DIMENSIONS, dtype=np.float32)
            vector[:2] = math.cos(angle), math.sin(angle)
            yield vector


def skill(identifier, description, compatibility=None):
    return Skill(
        id=identifier, name=identifier, slug=identifier, description=description,
        category="Research", author="Example", repository="example/skills",
        source_url="https://github.com/example/skills/blob/main/SKILL.md",
        revision=None, license=None, compatibility=compatibility, requirements=[], tags=[],
        workflow=[], output_style=[], evidence=[], install_commands={}, provenance="skillsmp",
    )


@pytest.fixture
def catalog(tmp_path):
    return CatalogStore(tmp_path / "catalog.sqlite3")


def ids(results):
    return [result.id for result in results]


def manifest(catalog):
    return json.loads((retrieval._paths(catalog) / "manifest.json").read_text())


def test_hybrid_finds_lexical_and_semantic_matches_and_caps_payload(catalog):
    catalog.upsert(skill("lexical", "stellar exact terminology"))
    catalog.upsert(skill("semantic", "nebula observation"))
    for number in range(80):
        catalog.upsert(skill(f"noise-{number:03}", "Accounting reports"))
    index_catalog(catalog, embedder=TestEmbedder())
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())

    results = retriever.search("stellar", "codex", [])

    assert len(results) == 40
    assert ids(results)[:2] == ["lexical", "semantic"]
    assert retriever.status() == {"indexed_count": 82, "retrieval_mode": "hybrid"}
    assert ids(retriever.search("stellar", "codex", [])) == ids(results)


def test_overfetch_filters_seen_and_incompatible_before_fusion(catalog):
    excluded = set()
    for number in range(330):
        identifier = f"excluded-{number:03}"
        catalog.upsert(skill(identifier, "nebula observation"))
        excluded.add(identifier)
    catalog.upsert(skill("wrong-agent", "stellar nebula", ["claude-code"]))
    catalog.upsert(skill("unknown-agent", "astronomy observation"))
    index_catalog(catalog, embedder=TestEmbedder())

    results = HybridRetriever(catalog, embedder=TestEmbedder()).search("stellar", "codex", excluded)

    assert ids(results) == ["unknown-agent"]


def test_stale_vectors_are_excluded_and_changed_records_get_new_ids(catalog):
    catalog.upsert(skill("changed", "nebula observation"))
    catalog.upsert(skill("unchanged", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())
    assert retriever.status()["indexed_count"] == 2
    with catalog.db_sessions() as session:
        original = dict(session.execute(select(
            SkillEmbeddingRecord.skill_id, SkillEmbeddingRecord.vector_id,
        )).all())
    catalog.upsert(skill("changed", "Accounting"))

    assert ids(retriever.search("stellar", "codex", [])) == ["unchanged"]
    assert retriever.status()["indexed_count"] == 1
    index_catalog(catalog, embedder=TestEmbedder())

    assert retriever.status()["indexed_count"] == 2
    with catalog.db_sessions() as session:
        current = dict(session.execute(select(
            SkillEmbeddingRecord.skill_id, SkillEmbeddingRecord.vector_id,
        ).join(CatalogSkillRecord, and_(
            CatalogSkillRecord.id == SkillEmbeddingRecord.skill_id,
            CatalogSkillRecord.text_hash == SkillEmbeddingRecord.text_hash,
        ))).all())
    assert current["changed"] != original["changed"]
    assert current["unchanged"] == original["unchanged"]
    published = manifest(catalog)
    index_catalog(catalog, embedder=TestEmbedder())
    assert manifest(catalog) == published


def test_failed_publication_preserves_old_index_and_resumes(catalog, monkeypatch):
    catalog.upsert(skill("original", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    previous = manifest(catalog)
    catalog.upsert(skill("added", "nebula observation"))
    publish = retrieval._publish

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(retrieval, "_publish", fail)
    with pytest.raises(OSError, match="disk full"):
        index_catalog(catalog, embedder=TestEmbedder())
    assert manifest(catalog) == previous
    assert ids(HybridRetriever(catalog, embedder=TestEmbedder()).search(
        "stellar", "codex", []
    )) == ["original"]
    with catalog.db_sessions() as session:
        persisted = dict(session.execute(select(
            SkillEmbeddingRecord.skill_id, SkillEmbeddingRecord.vector_id,
        )).all())
    monkeypatch.setattr(retrieval, "_publish", publish)
    index_catalog(catalog, embedder=TestEmbedder())
    with catalog.db_sessions() as session:
        assert dict(session.execute(select(
            SkillEmbeddingRecord.skill_id, SkillEmbeddingRecord.vector_id,
        )).all()) == persisted
    assert set(ids(HybridRetriever(catalog, embedder=TestEmbedder()).search(
        "stellar", "codex", []
    ))) == {"original", "added"}


@pytest.mark.parametrize("damage", ["model-fingerprint", "database-identity", "index", "manifest"])
def test_invalid_index_falls_back_to_keywords(catalog, damage):
    catalog.upsert(skill("lexical", "stellar terminology"))
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    root = retrieval._paths(catalog)
    data = manifest(catalog)
    if damage == "index":
        (root / data["filename"]).write_bytes(b"broken")
    elif damage == "manifest":
        (root / "manifest.json").write_text("{broken")
    else:
        key = "model_fingerprint" if damage == "model-fingerprint" else "database_identity"
        data[key] = "different"
        (root / "manifest.json").write_text(json.dumps(data))

    retriever = HybridRetriever(catalog, embedder=TestEmbedder())
    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]
    assert retriever.status() == {"indexed_count": 0, "retrieval_mode": "keyword-only"}


def test_model_change_reembeds_and_publishes_new_generation(catalog, monkeypatch):
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    previous = manifest(catalog)
    monkeypatch.setattr(retrieval, "MODEL_FINGERPRINT", "new-model-fingerprint")
    assert HybridRetriever(catalog, embedder=TestEmbedder()).status()["indexed_count"] == 0

    index_catalog(catalog, embedder=TestEmbedder())

    assert manifest(catalog)["generation"] != previous["generation"]
    assert HybridRetriever(catalog, embedder=TestEmbedder()).status()["indexed_count"] == 1
    with catalog.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(SkillEmbeddingRecord)) == 2


def test_restart_without_model_never_downloads_and_retains_index_count(catalog, monkeypatch):
    import httpx

    catalog.upsert(skill("lexical", "stellar terminology"))
    index_catalog(catalog, embedder=TestEmbedder())

    def no_network(*args, **kwargs):
        pytest.fail("Runtime attempted a model download")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(retrieval, "_load_embedder", no_network)
    retriever = HybridRetriever(CatalogStore(catalog.path))

    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]
    assert retriever.status() == {"indexed_count": 1, "retrieval_mode": "keyword-only"}
    with pytest.raises(ValueError, match="missing or damaged"):
        index_catalog(catalog, allow_download=False)


def test_empty_catalog_and_missing_index_remain_keyword_only(catalog):
    retriever = HybridRetriever(catalog)
    assert retriever.search("stellar", "codex", []) == []
    assert retriever.status() == {"indexed_count": 0, "retrieval_mode": "keyword-only"}
    assert index_catalog(catalog, allow_download=False)["indexed_count"] == 0
    catalog.upsert(skill("lexical", "stellar terminology"))
    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]


def test_reverted_metadata_is_not_counted_until_present_in_published_index(catalog):
    catalog.upsert(skill("one", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    catalog.upsert(skill("one", "Accounting reports"))
    index_catalog(catalog, embedder=TestEmbedder())
    catalog.upsert(skill("one", "nebula observation"))
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())

    assert retriever.status()["indexed_count"] == 0
    assert retriever.search("stellar", "codex", []) == []
    index_catalog(catalog, embedder=TestEmbedder())
    assert retriever.status()["indexed_count"] == 1
    assert ids(retriever.search("stellar", "codex", [])) == ["one"]


def test_model_checksum_damage_disables_semantic_search_without_network(catalog, monkeypatch):
    import hashlib

    catalog.upsert(skill("lexical", "stellar terminology"))
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    model_dir = retrieval._paths(catalog) / "model"
    model_dir.mkdir()
    model_file = model_dir / "tiny.onnx"
    model_file.write_bytes(b"valid")
    monkeypatch.setattr(retrieval, "MODEL_FILES", {
        "tiny.onnx": hashlib.sha256(b"valid").hexdigest(),
    })
    monkeypatch.setattr(retrieval, "_load_embedder", lambda directory: TestEmbedder())
    retriever = HybridRetriever(catalog)
    assert "semantic" in ids(retriever.search("stellar", "codex", []))
    model_file.write_bytes(b"wrong")

    assert retriever.status() == {"indexed_count": 2, "retrieval_mode": "keyword-only"}
    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]
