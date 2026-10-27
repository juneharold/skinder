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
def catalog(db, settings, tmp_path):
    return CatalogStore(db, settings.model_copy(update={"embedding_model_dir": tmp_path / "model"}))


def ids(results):
    return [result.id for result in results]


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
    index_catalog(catalog, embedder=TestEmbedder())
    with catalog.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(SkillEmbeddingRecord)) == 3


def test_interrupted_batch_preserves_committed_vectors_and_resumes(catalog):
    catalog.settings = catalog.settings.model_copy(update={"embedding_batch_size": 1})
    catalog.upsert(skill("a", "nebula observation"))
    catalog.upsert(skill("b", "nebula observation"))

    class InterruptedEmbedder(TestEmbedder):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Model interrupted")
            return super().embed(texts)

    with pytest.raises(RuntimeError, match="interrupted"):
        index_catalog(catalog, embedder=InterruptedEmbedder())
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())
    assert retriever.status()["indexed_count"] == 1
    assert ids(retriever.search("stellar", "codex", [])) == ["a"]
    with catalog.db_sessions() as session:
        first_id = session.scalar(select(SkillEmbeddingRecord.vector_id))
    index_catalog(catalog, embedder=TestEmbedder())
    assert set(ids(retriever.search("stellar", "codex", []))) == {"a", "b"}
    with catalog.db_sessions() as session:
        assert session.scalar(select(SkillEmbeddingRecord.vector_id).where(
            SkillEmbeddingRecord.skill_id == "a")) == first_id


def test_incompatible_model_vectors_fall_back_to_keywords(catalog, monkeypatch):
    catalog.upsert(skill("lexical", "stellar terminology"))
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    monkeypatch.setattr(retrieval, "MODEL_FINGERPRINT", "different")
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())
    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]
    assert retriever.status() == {"indexed_count": 0, "retrieval_mode": "keyword-only"}


def test_model_change_reembeds_with_new_fingerprint(catalog, monkeypatch):
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    monkeypatch.setattr(retrieval, "MODEL_FINGERPRINT", "new-model-fingerprint")
    assert HybridRetriever(catalog, embedder=TestEmbedder()).status()["indexed_count"] == 0

    index_catalog(catalog, embedder=TestEmbedder())

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
    retriever = HybridRetriever(CatalogStore(catalog.database, catalog.settings))

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


def test_reverted_metadata_reuses_compatible_persisted_vector(catalog):
    catalog.upsert(skill("one", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    catalog.upsert(skill("one", "Accounting reports"))
    index_catalog(catalog, embedder=TestEmbedder())
    catalog.upsert(skill("one", "nebula observation"))
    retriever = HybridRetriever(catalog, embedder=TestEmbedder())

    assert retriever.status()["indexed_count"] == 1
    assert ids(retriever.search("stellar", "codex", [])) == ["one"]
    index_catalog(catalog, embedder=TestEmbedder())
    assert retriever.status()["indexed_count"] == 1
    assert ids(retriever.search("stellar", "codex", [])) == ["one"]


def test_model_checksum_damage_disables_semantic_search_without_network(catalog, monkeypatch):
    import hashlib

    catalog.upsert(skill("lexical", "stellar terminology"))
    catalog.upsert(skill("semantic", "nebula observation"))
    index_catalog(catalog, embedder=TestEmbedder())
    model_dir = catalog.settings.embedding_model_dir
    model_dir.mkdir()
    model_file = model_dir / "tiny.onnx"
    model_file.write_bytes(b"valid")
    monkeypatch.setattr(retrieval, "MODEL_FILES", {
        "tiny.onnx": hashlib.sha256(b"valid").hexdigest(),
    })
    monkeypatch.setattr(retrieval, "_load_embedder", lambda directory, threads: TestEmbedder())
    retriever = HybridRetriever(catalog)
    assert "semantic" in ids(retriever.search("stellar", "codex", []))
    model_file.write_bytes(b"wrong")

    assert retriever.status() == {"indexed_count": 2, "retrieval_mode": "keyword-only"}
    assert ids(retriever.search("stellar", "codex", [])) == ["lexical"]
