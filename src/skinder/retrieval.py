"""Local hybrid retrieval; only explicit indexing may download embedding artifacts."""

import hashlib
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import numpy as np
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.sqlite import insert
from usearch.index import Index

from .orm import (
    CatalogSkillRecord,
    RetrievalMemberRecord,
    RetrievalMetaRecord,
    SkillEmbeddingRecord,
)

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
MODEL_FINGERPRINT = hashlib.sha256(json.dumps({
    "model": MODEL, "revision": REVISION, "files": MODEL_FILES,
    "dimensions": DIMENSIONS, "pooling": "mean", "normalization": "l2",
    "query_prefix": "query: ", "document_prefix": "passage: ", "max_tokens": 512,
}, sort_keys=True).encode()).hexdigest()
logger = logging.getLogger(__name__)
_model_lock = threading.Lock()


def _paths(catalog):
    return catalog.path.with_name(catalog.path.stem + "-search")


def _identity(catalog):
    with catalog.db_sessions() as session:
        return session.get(RetrievalMetaRecord, "identity").value


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@lru_cache(maxsize=32)
def _verified_file(path, size, modified, expected):
    # Size and modification time invalidate the cached verification after a file changes.
    return _sha256(path) == expected


def _verified(path, expected):
    try:
        stat = path.stat()
        return _verified_file(path, stat.st_size, stat.st_mtime_ns, expected)
    except OSError:
        return False


def _model_ready(directory):
    return all(_verified(directory / name, digest) for name, digest in MODEL_FILES.items())


def _prepare_model(directory, allow_download):
    if _model_ready(directory):
        return
    if not allow_download:
        raise ValueError("Local embedding model is missing or damaged. Run catalog indexing.")
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
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)


def _load_embedder(directory):
    # The explicit directory and local_files_only both prevent FastEmbed's automatic downloads.
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    with _model_lock:
        if not any(item["model"] == MODEL for item in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(
                model=MODEL, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL), dim=DIMENSIONS, model_file="onnx/model.onnx",
            )
    return TextEmbedding(
        model_name=MODEL, specific_model_path=str(directory), local_files_only=True,
        providers=["CPUExecutionProvider"], threads=4,
    )


def _embed(embedder, texts):
    vectors = np.asarray(list(embedder.embed(texts)), dtype=np.float32)
    if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
        raise ValueError("Embedding model returned invalid vectors.")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding model returned an empty vector.")
    return vectors / norms


def _manifest(root, identity):
    try:
        value = json.loads((root / "manifest.json").read_text())
        if (
            value["database_identity"] != identity
            or value["model_fingerprint"] != MODEL_FINGERPRINT
            or value["dimensions"] != DIMENSIONS
            or value["format"] != 1
            or value["filename"] != f"{value['generation']}.usearch"
            or Path(value["filename"]).name != value["filename"]
        ):
            return None
        if not isinstance(value["count"], int) or value["count"] < 0:
            return None
        if not isinstance(value["sha256"], str) or len(value["sha256"]) != 64:
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _indexed_count(catalog, generation):
    with catalog.db_sessions() as session:
        return session.scalar(select(func.count()).select_from(RetrievalMemberRecord).join(
            SkillEmbeddingRecord,
            SkillEmbeddingRecord.vector_id == RetrievalMemberRecord.vector_id,
        ).join(CatalogSkillRecord, and_(
            CatalogSkillRecord.id == SkillEmbeddingRecord.skill_id,
            CatalogSkillRecord.text_hash == SkillEmbeddingRecord.text_hash,
        )).where(
            RetrievalMemberRecord.generation == generation,
            SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT,
        ))


def _publish(root, manifest):
    temporary = root / "manifest.json.part"
    try:
        with temporary.open("w") as output:
            json.dump(manifest, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)


def index_catalog(catalog, *, allow_download=True, embedder=None):
    """Resume embeddings, then publish a complete index. Caller owns the writer lock.

    Tests may supply an embedder with an ``embed(texts)`` method; production uses pinned E5.
    """
    identity = _identity(catalog)
    root = _paths(catalog)
    root.mkdir(parents=True, exist_ok=True)
    if not catalog.count():
        return {"indexed_count": 0, "retrieval_mode": "keyword-only"}
    if embedder is None:
        _prepare_model(root / "model", allow_download)
    previous = _manifest(root, identity)
    if previous and _verified(root / previous["filename"], previous["sha256"]):
        count = _indexed_count(catalog, previous["generation"])
        if count == previous["count"] == catalog.count():
            return {"indexed_count": count, "retrieval_mode": "hybrid"}
    if embedder is None:
        embedder = _load_embedder(root / "model")

    # Keyset pagination bounds memory and resumes from persisted embeddings after interruption.
    last_id = ""
    while True:
        with catalog.db_sessions() as session:
            rows = session.execute(select(
                CatalogSkillRecord.id, CatalogSkillRecord.text_hash,
                CatalogSkillRecord.retrieval_text,
            ).where(
                CatalogSkillRecord.id > last_id,
                ~select(SkillEmbeddingRecord.vector_id).where(
                    SkillEmbeddingRecord.skill_id == CatalogSkillRecord.id,
                    SkillEmbeddingRecord.text_hash == CatalogSkillRecord.text_hash,
                    SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT,
                ).exists(),
            ).order_by(CatalogSkillRecord.id).limit(64)).all()
        if not rows:
            break
        vectors = _embed(embedder, ["passage: " + row.retrieval_text for row in rows])
        with catalog.db_sessions.begin() as session:
            session.execute(insert(SkillEmbeddingRecord).on_conflict_do_nothing(
                index_elements=["skill_id", "text_hash", "model_fingerprint"],
            ), [{"skill_id": row.id, "text_hash": row.text_hash,
                 "model_fingerprint": MODEL_FINGERPRINT, "vector": vector.tobytes()}
                for row, vector in zip(rows, vectors, strict=True)])
        last_id = rows[-1].id

    generation = uuid4().hex
    filename = f"{generation}.usearch"
    index = Index(ndim=DIMENSIONS, metric="cos", dtype="f16", connectivity=16,
                  expansion_add=128, expansion_search=256)
    try:
        last_id = 0
        while True:
            with catalog.db_sessions() as session:
                rows = session.execute(select(
                    SkillEmbeddingRecord.vector_id, SkillEmbeddingRecord.vector,
                ).join(CatalogSkillRecord, and_(
                    CatalogSkillRecord.id == SkillEmbeddingRecord.skill_id,
                    CatalogSkillRecord.text_hash == SkillEmbeddingRecord.text_hash,
                )).where(
                    SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT,
                    SkillEmbeddingRecord.vector_id > last_id,
                ).order_by(SkillEmbeddingRecord.vector_id).limit(1024)).all()
            if not rows:
                break
            keys = np.array([row.vector_id for row in rows], dtype=np.uint64)
            vectors = np.stack([np.frombuffer(row.vector, dtype=np.float32) for row in rows])
            index.add(keys, vectors, threads=4)
            with catalog.db_sessions.begin() as session:
                session.add_all(RetrievalMemberRecord(generation=generation, vector_id=int(key))
                                for key in keys)
            last_id = int(keys[-1])
        index.save(root / filename)
        with (root / filename).open("rb") as stream:
            os.fsync(stream.fileno())
        manifest = {
            "format": 1, "database_identity": identity, "model_fingerprint": MODEL_FINGERPRINT,
            "dimensions": DIMENSIONS, "generation": generation, "filename": filename,
            "count": len(index), "sha256": _sha256(root / filename),
        }
        _publish(root, manifest)
    except BaseException:
        (root / filename).unlink(missing_ok=True)
        with catalog.db_sessions.begin() as session:
            session.execute(delete(RetrievalMemberRecord).where(
                RetrievalMemberRecord.generation == generation
            ))
        raise

    # Keep one previous generation; mapped readers on this Mac retain their open old files.
    keep = [generation] + ([previous["generation"]] if previous else [])
    with catalog.db_sessions.begin() as session:
        session.execute(delete(RetrievalMemberRecord).where(
            RetrievalMemberRecord.generation.not_in(keep)
        ))
    for path in root.glob("*.usearch"):
        if path.stem not in keep:
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not remove retired vector index %s", path)
    return {"indexed_count": _indexed_count(catalog, generation), "retrieval_mode": "hybrid"}


class HybridRetriever:
    def __init__(self, catalog, *, embedder=None):
        self.catalog = catalog
        self.root = _paths(catalog)
        self.identity = _identity(catalog)
        self._embedder = embedder
        self._injected = embedder is not None
        self._index = None
        self._generation = None
        self._lock = threading.RLock()
        self._model_failed = False
        self._count_key = None
        self._count = 0

    def _resources(self):
        manifest = _manifest(self.root, self.identity)
        if not manifest:
            return None
        path = self.root / manifest["filename"]
        if not _verified(path, manifest["sha256"]):
            return None
        with self._lock:
            if self._generation != manifest["generation"]:
                index = Index.restore(path, view=True)
                if index is None or len(index) != manifest["count"] or index.ndim != DIMENSIONS:
                    return None
                index.expansion_search = 256
                self._index = index
                self._generation = manifest["generation"]
            return self._index, manifest

    def status(self):
        try:
            resources = self._resources()
            if resources:
                generation = resources[1]["generation"]
                with self.catalog.db_sessions() as session:
                    revision = session.get(RetrievalMetaRecord, "catalog_revision").value
                with self._lock:
                    if self._count_key != (generation, revision):
                        self._count = _indexed_count(self.catalog, generation)
                        self._count_key = (generation, revision)
                    count = self._count
                ready = self._injected or _model_ready(self.root / "model")
                return {"indexed_count": count, "retrieval_mode": (
                    "hybrid" if count and ready and not self._model_failed else "keyword-only"
                )}
        except Exception:
            logger.warning("Local vector index is unavailable", exc_info=True)
        return {"indexed_count": 0, "retrieval_mode": "keyword-only"}

    def _vector_skills(self, keys, agent, excluded):
        ids = []
        with self.catalog.db_sessions() as session:
            for start in range(0, len(keys), 500):
                batch = [int(key) for key in keys[start:start + 500]]
                rows = session.execute(select(
                    SkillEmbeddingRecord.vector_id, SkillEmbeddingRecord.skill_id,
                ).join(CatalogSkillRecord, and_(
                    CatalogSkillRecord.id == SkillEmbeddingRecord.skill_id,
                    CatalogSkillRecord.text_hash == SkillEmbeddingRecord.text_hash,
                )).where(
                    SkillEmbeddingRecord.model_fingerprint == MODEL_FINGERPRINT,
                    SkillEmbeddingRecord.vector_id.in_(batch),
                )).all()
                found = {row.vector_id: row.skill_id for row in rows}
                ids.extend(found[key] for key in batch if key in found)
        return self.catalog.eligible(ids, agent=agent, excluded=excluded)

    def search(self, task, agent, excluded):
        excluded = set(excluded)
        lexical = self.catalog.keyword_search(task, agent=agent, excluded=excluded, limit=200)
        semantic = []
        try:
            resources = self._resources()
            if resources and (self._injected or _model_ready(self.root / "model")):
                index, _ = resources
                with self._lock:
                    try:
                        if self._embedder is None:
                            self._embedder = _load_embedder(self.root / "model")
                        query = _embed(self._embedder, ["query: " + task])[0]
                        self._model_failed = False
                    except Exception:
                        self._model_failed = True
                        raise
                for count in (256, 1024, 4096):
                    matches = index.search(
                        query, count=min(count, len(index)), threads=1, exact=len(index) <= 4096,
                    )
                    ordered = sorted(zip(matches.keys, matches.distances, strict=True),
                                     key=lambda item: (item[1], item[0]))
                    semantic = self._vector_skills([key for key, _ in ordered], agent, excluded)[:200]
                    if len(semantic) >= 200 or count >= len(index):
                        break
        except Exception:
            logger.warning("Using keyword search because local semantic search failed", exc_info=True)
        scores, skills = {}, {}
        for results in (lexical, semantic):
            for rank, skill in enumerate(results, 1):
                skills[skill.id] = skill
                scores[skill.id] = scores.get(skill.id, 0) + 1 / (60 + rank)
        return [skills[key] for key in sorted(scores, key=lambda key: (-scores[key], key))[:40]]
