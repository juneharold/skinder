"""PostgreSQL hybrid retrieval with a pinned local E5 embedding model."""

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, TypedDict

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert

from .catalog import CatalogStore, eligibility
from .models import Agent, Skill
from .orm import CatalogSkillRecord, SkillEmbeddingRecord
from .settings import Settings

MODEL = "intfloat/multilingual-e5-small"
# Pinned from the model publisher's Hugging Face revision and artifact metadata.
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_FILES = {
    "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
    "special_tokens_map.json": "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
    "tokenizer_config.json": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
}
DIMENSIONS = 384
# Stored vectors are reusable only with the same artifacts and embedding recipe.
MODEL_FINGERPRINT = hashlib.sha256(json.dumps({
    "model": MODEL, "revision": REVISION, "files": MODEL_FILES,
    "dimensions": DIMENSIONS, "pooling": "mean", "normalization": "l2",
    "query_prefix": "query: ", "document_prefix": "passage: ", "max_tokens": 512,
}, sort_keys=True).encode()).hexdigest()
logger = logging.getLogger(__name__)
_model_lock = threading.Lock()


class Embedder(Protocol):
    def embed(self, texts: Sequence[str], /) -> Iterable[NDArray[np.float32]]: ...


class RetrievalStatus(TypedDict):
    indexed_count: int
    retrieval_mode: Literal["hybrid", "keyword-only"]


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@lru_cache(maxsize=32)
def _verified_file(path: Path, size: int, modified: int, expected: str) -> bool:
    # File metadata is part of the cache key, so normal replacements trigger another hash.
    return _sha256(path) == expected


def _verified(path: Path, expected: str) -> bool:
    try:
        stat = path.stat()
        return _verified_file(path, stat.st_size, stat.st_mtime_ns, expected)
    except OSError:
        return False


def _model_ready(directory: Path) -> bool:
    # Tokenizer and configuration must match the weights, not merely exist beside them.
    return all(_verified(directory / name, digest) for name, digest in MODEL_FILES.items())


def _prepare_model(directory: Path, allow_download: bool) -> None:
    if _model_ready(directory):
        return
    if not allow_download:
        raise ValueError("Local embedding model is missing or damaged. Rebuild the worker image.")
    import httpx

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for name, digest in MODEL_FILES.items():
            target = directory / name
            if _verified(target, digest):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}"
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                if _sha256(temporary) != digest:
                    raise ValueError(f"Embedding model checksum mismatch: {name}")
                # Interrupted or corrupt downloads must not replace the target artifact.
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)


def _load_embedder(directory: Path, threads: int = 4) -> Embedder:
    # The explicit directory and local_files_only both prevent FastEmbed's automatic downloads.
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    # Concurrent retrievers must not register the same custom model twice.
    with _model_lock:
        if not any(item["model"] == MODEL for item in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(
                model=MODEL, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL), dim=DIMENSIONS, model_file="onnx/model.onnx",
            )
    return TextEmbedding(
        model_name=MODEL, specific_model_path=str(directory), local_files_only=True,
        providers=["CPUExecutionProvider"], threads=threads,
    )


def _embed(embedder: Embedder, texts: Sequence[str]) -> NDArray[np.float32]:
    # Reject unusable output before it reaches stored embeddings or the native index.
    vectors = np.asarray(list(embedder.embed(texts)), dtype=np.float32)
    if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
        raise ValueError("Embedding model returned invalid vectors.")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding model returned an empty vector.")
    return vectors / norms


def prepare_model(directory: str | Path) -> None:
    """Download and verify pinned artifacts during image construction."""
    _prepare_model(Path(directory), allow_download=True)


def _current_vectors():
    return select(SkillEmbeddingRecord).join(CatalogSkillRecord, and_(
        CatalogSkillRecord.id == SkillEmbeddingRecord.skill_id,
        CatalogSkillRecord.text_hash == SkillEmbeddingRecord.text_hash,
    )).where(SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT)


def _indexed_count(catalog: CatalogStore) -> int:
    with catalog.db_sessions() as session:
        return session.scalar(select(func.count()).select_from(_current_vectors().subquery()))


def index_catalog(catalog: CatalogStore, *, allow_download: bool = False,
                  embedder: Embedder | None = None) -> RetrievalStatus:
    """Persist bounded batches; completed batches remain queryable after interruption.

    The ingest command owns the shared advisory lock across synchronization and indexing.
    """
    if not catalog.count():
        return {"indexed_count": 0, "retrieval_mode": "keyword-only"}
    settings = catalog.settings
    if embedder is None:
        _prepare_model(settings.embedding_model_dir, allow_download)
        embedder = _load_embedder(settings.embedding_model_dir, settings.embedding_threads)
    last_id = ""
    while True:
        with catalog.db_sessions() as session:
            rows = session.execute(select(CatalogSkillRecord.id, CatalogSkillRecord.text_hash,
                CatalogSkillRecord.retrieval_text).where(
                CatalogSkillRecord.id > last_id,
                ~select(SkillEmbeddingRecord.vector_id).where(
                    SkillEmbeddingRecord.skill_id == CatalogSkillRecord.id,
                    SkillEmbeddingRecord.text_hash == CatalogSkillRecord.text_hash,
                    SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT,
                ).exists(),
            ).order_by(CatalogSkillRecord.id).limit(settings.embedding_batch_size)).all()
        if not rows:
            break
        vectors = _embed(embedder, ["passage: " + row.retrieval_text for row in rows])
        with catalog.db_sessions.begin() as session:
            session.execute(insert(SkillEmbeddingRecord).on_conflict_do_nothing(
                index_elements=["skill_id", "text_hash", "model_fingerprint"],
            ), [{"skill_id": row.id, "text_hash": row.text_hash,
                 "model_fingerprint": MODEL_FINGERPRINT, "vector": vector}
                for row, vector in zip(rows, vectors, strict=True)])
        last_id = rows[-1].id
    count = _indexed_count(catalog)
    return {"indexed_count": count, "retrieval_mode": "hybrid" if count else "keyword-only"}


class HybridRetriever:
    def __init__(self, catalog: CatalogStore, settings: Settings | None = None, *,
                 embedder: Embedder | None = None) -> None:
        self.catalog = catalog
        self.settings = settings or catalog.settings
        self._embedder = embedder
        self._injected = embedder is not None
        self._lock = threading.RLock()
        self._model_failed = False

    def status(self) -> RetrievalStatus:
        count = _indexed_count(self.catalog)
        ready = self._injected or _model_ready(self.settings.embedding_model_dir)
        return {"indexed_count": count, "retrieval_mode": (
            "hybrid" if count and ready and not self._model_failed else "keyword-only")}

    def _semantic(self, vector, agent, excluded):
        distance = SkillEmbeddingRecord.vector.cosine_distance(vector)
        statement = eligibility(_current_vectors(), agent, excluded).with_only_columns(
            CatalogSkillRecord.data, distance.label("distance"))
        small_catalog = self.catalog.count() <= 4096
        with self.catalog.db_sessions.begin() as session:
            # Small catalogs use exact distances. Large catalogs retain the HNSW distance-only
            # order so PostgreSQL can use its index, with bounded iterative filtering.
            if small_catalog:
                session.execute(text("SET LOCAL enable_indexscan = off"))
                statement = statement.order_by(distance, CatalogSkillRecord.id).limit(200)
            else:
                session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
                session.execute(text("SET LOCAL hnsw.ef_search = 256"))
                session.execute(text("SET LOCAL hnsw.max_scan_tuples = 20000"))
                statement = statement.order_by(distance).limit(200)
            rows = session.execute(statement).all()
        rows = sorted(rows, key=lambda row: (row.distance, row.data["id"]))
        return [Skill.model_validate(row.data) for row in rows]

    def search(self, task: str, agent: Agent, excluded: Iterable[str]) -> list[Skill]:
        excluded = set(excluded)
        lexical = self.catalog.keyword_search(task, agent=agent, excluded=excluded, limit=200)
        semantic = []
        if self._injected or _model_ready(self.settings.embedding_model_dir):
            try:
                with self._lock:
                    try:
                        if self._embedder is None:
                            self._embedder = _load_embedder(self.settings.embedding_model_dir,
                                                           self.settings.embedding_threads)
                        query = _embed(self._embedder, ["query: " + task])[0]
                        self._model_failed = False
                    except Exception:
                        self._model_failed = True
                        raise
                semantic = self._semantic(query, agent, excluded)
            except Exception as exc:  # noqa: BLE001 - semantic failures retain keyword retrieval
                # Exception bodies can contain SQL parameters or private task text.
                logger.warning("Semantic search unavailable (%s)", type(exc).__name__)
        scores, skills = {}, {}
        for results in (lexical, semantic):
            for rank, skill in enumerate(results, 1):
                skills[skill.id] = skill
                scores[skill.id] = scores.get(skill.id, 0) + 1 / (60 + rank)
        return [skills[key] for key in sorted(scores, key=lambda key: (-scores[key], key))[:40]]
