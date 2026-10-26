"""Persistent catalog and keyword retrieval, independent of personal task history."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .models import Catalog, Skill

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_CATALOG = Path(__file__).parent / "data" / "catalog.json"


def default_catalog_path():
    return Path(os.environ.get("SKINDER_CATALOG_DB_PATH", ROOT / ".data" / "catalog.sqlite3"))


def stable_id(repository, skill_path):
    directory = str(PurePosixPath(skill_path).parent)
    return hashlib.sha256(f"{repository.lower()}:{directory}".encode()).hexdigest()[:20]


def retrieval_text(skill):
    return "\n".join([
        skill.name, skill.description, skill.category, " ".join(skill.tags),
        *skill.requirements, *skill.workflow, *skill.output_style,
    ])


def keyword_query(query):
    # Quote tokens, rather than exposing FTS operators or SQL to user input.
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))[:64]
    return " OR ".join(f'"{token}"' for token in tokens)


class CatalogStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog_skills (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, provenance TEXT NOT NULL,
                    name TEXT NOT NULL, category TEXT NOT NULL, compatibility TEXT,
                    retrieval_text TEXT NOT NULL, text_hash TEXT NOT NULL,
                    source_updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS catalog_category ON catalog_skills(category);
                CREATE INDEX IF NOT EXISTS catalog_name ON catalog_skills(name COLLATE NOCASE, id);
                CREATE TABLE IF NOT EXISTS catalog_aliases (
                    provider TEXT NOT NULL, external_id TEXT NOT NULL, skill_id TEXT NOT NULL,
                    payload TEXT, PRIMARY KEY (provider, external_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                    skill_id UNINDEXED, name, description, tags, tokenize='unicode61'
                );
            """)

    @contextmanager
    def connect(self):
        # Every operation owns its connection, including worker-thread searches.
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed(self, path=BUNDLED_CATALOG):
        path = Path(path)
        if not path.exists():
            return
        skills = [Skill.model_validate(row) for row in json.loads(path.read_text())["skills"]]
        if len({skill.id for skill in skills}) != len(skills):
            raise ValueError("Catalog has duplicate skill IDs.")
        for skill in skills:
            self.upsert(skill)

    def upsert(self, skill, *, provider_id=None, raw=None, source_updated_at=None):
        text = retrieval_text(skill)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        payload = skill.model_dump_json(by_alias=True)
        source_updated_at = str(source_updated_at) if source_updated_at is not None else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM catalog_skills WHERE id=?", (skill.id,)
            ).fetchone()
            if provider_id is not None:
                db.execute(
                    "INSERT INTO catalog_aliases VALUES (?,?,?,?) "
                    "ON CONFLICT(provider, external_id) DO UPDATE SET "
                    "skill_id=excluded.skill_id,payload=excluded.payload",
                    ("skillsmp", provider_id, skill.id, json.dumps(raw, ensure_ascii=False)),
                )
            if previous and previous["provenance"] == "bundled" and skill.provenance != "bundled":
                return "unchanged"
            if previous and previous["data"] == payload and previous["source_updated_at"] == source_updated_at:
                return "unchanged"
            db.execute(
                "INSERT INTO catalog_skills VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data,provenance=excluded.provenance,"
                "name=excluded.name,category=excluded.category,compatibility=excluded.compatibility,"
                "retrieval_text=excluded.retrieval_text,text_hash=excluded.text_hash,"
                "source_updated_at=excluded.source_updated_at",
                (skill.id, payload, skill.provenance, skill.name, skill.category,
                 json.dumps(skill.compatibility) if skill.compatibility is not None else None,
                 text, text_hash, source_updated_at),
            )
            db.execute("DELETE FROM skills_fts WHERE skill_id=?", (skill.id,))
            db.execute(
                "INSERT INTO skills_fts VALUES (?,?,?,?)",
                (skill.id, skill.name, skill.description, " ".join(skill.tags)),
            )
        return "updated" if previous else "added"

    def get(self, skill_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM catalog_skills WHERE id=?", (skill_id,)).fetchone()
        return Skill.model_validate_json(row[0]) if row else None

    def get_many(self, ids):
        ids = list(dict.fromkeys(ids))
        found = {}
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                rows = db.execute(
                    "SELECT id,data FROM catalog_skills WHERE id IN ("
                    + ",".join("?" for _ in batch) + ")", batch,
                )
                found.update((row["id"], Skill.model_validate_json(row["data"])) for row in rows)
        return [found[skill_id] for skill_id in ids if skill_id in found]

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM catalog_skills").fetchone()[0]

    def page(self, *, q="", category=None, offset=0, limit=50):
        match = keyword_query(q)
        clauses, params = [], []
        join, order = "", "s.name COLLATE NOCASE,s.id"
        if q.strip():
            if not match:
                clauses.append("0")
            else:
                join = " JOIN skills_fts ON skills_fts.skill_id=s.id"
                clauses.append("skills_fts MATCH ?")
                params.append(match)
                order = "bm25(skills_fts,0,4,1,2),s.rowid"
        if category:
            clauses.append("s.category=?")
            params.append(category)
        base = " FROM catalog_skills s" + join
        if clauses:
            base += " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            total = db.execute("SELECT count(*)" + base, params).fetchone()[0]
            rows = db.execute(
                "SELECT s.data" + base + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            categories = [row[0] for row in db.execute(
                "SELECT DISTINCT category FROM catalog_skills ORDER BY category"
            )]
        return Catalog(
            skills=[Skill.model_validate_json(row[0]) for row in rows], categories=categories,
            total=total, has_more=offset + len(rows) < total,
        )

    def keyword_search(self, query, *, agent=None, excluded=(), limit=200):
        match = keyword_query(query)
        if not match:
            return []
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE exclusions(id TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO exclusions VALUES (?)", ((x,) for x in excluded))
            rows = db.execute(
                "SELECT s.data FROM skills_fts JOIN catalog_skills s ON s.id=skills_fts.skill_id "
                "WHERE skills_fts MATCH ? AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.id=s.id) "
                "AND (? IS NULL OR s.compatibility IS NULL OR EXISTS "
                "(SELECT 1 FROM json_each(s.compatibility) WHERE value=?)) "
                "ORDER BY bm25(skills_fts,0,4,1,2),s.rowid LIMIT ?",
                (match, agent, agent, limit),
            ).fetchall()
        return [Skill.model_validate_json(row[0]) for row in rows]

    def eligible(self, ids, *, agent=None, excluded=()):
        excluded = set(excluded)
        return [skill for skill in self.get_many(ids) if skill.id not in excluded and (
            agent is None or skill.compatibility is None or agent in skill.compatibility
        )]
