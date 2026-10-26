"""Persistent catalog and keyword retrieval, independent of personal task history."""

import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import column, func, literal_column, or_, select, table, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from .database import create_database
from .models import Agent, Catalog, Skill
from .orm import CatalogAliasRecord, CatalogSkillRecord

ROOT = Path(__file__).resolve().parents[2]


def default_catalog_db_path() -> Path:
    return Path(os.environ.get("SKINDER_CATALOG_DB_PATH", ROOT / ".data" / "catalog.sqlite3"))


def stable_id(repository: str, skill_path: str) -> str:
    # A skill keeps its identity when its source branch or revision changes.
    directory = str(PurePosixPath(skill_path).parent)
    return hashlib.sha256(f"{repository.lower()}:{directory}".encode()).hexdigest()[:20]


def retrieval_text(skill: Skill) -> str:
    # These fields define embedding content; changes elsewhere need no new vector.
    return "\n".join([
        skill.name, skill.description, skill.category, " ".join(skill.tags),
        *skill.requirements, *skill.workflow, *skill.output_style,
    ])


def keyword_query(query: str) -> str:
    # Treat task words literally instead of accepting FTS operators; cap work for long tasks.
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))[:64]
    return " OR ".join(f'"{token}"' for token in tokens)


# FTS5 virtual tables have no ORM mapping; MATCH, ranking, and FTS writes use
# SQLite expressions while catalog records are read and written through the ORM.
skills_fts = table("skills_fts", column("skill_id"))


class CatalogStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.engine = create_database(self.path, "catalog")
        # Each operation owns its session, including worker-thread searches.
        self.db_sessions = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()

    def upsert(
        self, skill: Skill, *, provider_id: str | None = None,
        raw: dict[str, Any] | None = None, source_updated_at: str | float | None = None,
    ) -> Literal["added", "updated", "unchanged"]:
        content = retrieval_text(skill)
        text_hash = hashlib.sha256(content.encode()).hexdigest()
        payload = skill.model_dump_json(by_alias=True)
        source_updated_at = str(source_updated_at) if source_updated_at is not None else None
        with self.db_sessions.begin() as session:
            # Serialize the read/modify/write decision and its FTS update across writers.
            session.connection(execution_options={"sqlite_begin_mode": "IMMEDIATE"})
            previous = session.get(CatalogSkillRecord, skill.id)
            # Save provider observations even when existing catalog data is retained.
            if provider_id is not None:
                statement = insert(CatalogAliasRecord).values(
                    provider="skillsmp", external_id=provider_id, skill_id=skill.id,
                    payload=json.dumps(raw, ensure_ascii=False),
                )
                session.execute(statement.on_conflict_do_update(
                    index_elements=["provider", "external_id"],
                    set_={"skill_id": statement.excluded.skill_id,
                          "payload": statement.excluded.payload},
                ))
            # Provider listings cannot replace reviewed bundled evidence with unknown metadata.
            if previous and previous.provenance == "bundled" and skill.provenance != "bundled":
                return "unchanged"
            if previous and previous.data == payload and previous.source_updated_at == source_updated_at:
                return "unchanged"
            record = previous or CatalogSkillRecord(id=skill.id)
            record.data = payload
            record.provenance = skill.provenance
            record.name = skill.name
            record.category = skill.category
            record.compatibility = (
                json.dumps(skill.compatibility) if skill.compatibility is not None else None
            )
            record.retrieval_text = content
            record.text_hash = text_hash
            record.source_updated_at = source_updated_at
            session.add(record)
            session.flush()
            session.execute(text("DELETE FROM skills_fts WHERE skill_id=:id"), {"id": skill.id})
            session.execute(text(
                "INSERT INTO skills_fts (skill_id,name,description,tags,author) "
                "VALUES (:id,:name,:description,:tags,:author)"
            ), {"id": skill.id, "name": skill.name, "description": skill.description,
                "tags": " ".join(skill.tags), "author": skill.author})
        return "updated" if previous else "added"

    def get(self, skill_id: str) -> Skill | None:
        with self.db_sessions() as session:
            row = session.get(CatalogSkillRecord, skill_id)
            return Skill.model_validate_json(row.data) if row else None

    def get_many(self, ids: Iterable[str]) -> list[Skill]:
        ids = list(dict.fromkeys(ids))
        found = {}
        with self.db_sessions() as session:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                rows = session.execute(select(CatalogSkillRecord.id, CatalogSkillRecord.data).where(
                    CatalogSkillRecord.id.in_(batch)
                ))
                found.update((row.id, Skill.model_validate_json(row.data)) for row in rows)
        # SQL IN has no rank order; reconstruct the caller's order after batching the reads.
        return [found[skill_id] for skill_id in ids if skill_id in found]

    def count(self) -> int:
        with self.db_sessions() as session:
            return session.execute(select(func.count()).select_from(CatalogSkillRecord)).scalar_one()

    def page(
        self, *, q: str = "", category: str | None = None, offset: int = 0, limit: int = 50,
    ) -> Catalog:
        match = keyword_query(q)
        statement = select(CatalogSkillRecord.data)
        order = (CatalogSkillRecord.name.collate("NOCASE"), CatalogSkillRecord.id)
        params = {}
        if match:
            statement = statement.join(
                skills_fts, skills_fts.c.skill_id == CatalogSkillRecord.id,
            ).where(text("skills_fts MATCH :match"))
            params["match"] = match
            order = (literal_column("bm25(skills_fts,0,4,1,2,2)"),
                     literal_column("catalog_skills.rowid"))
        elif q.strip():
            # Blank input lists the catalog; punctuation alone must not match everything.
            statement = statement.where(False)
        if category:
            statement = statement.where(CatalogSkillRecord.category == category)
        with self.db_sessions() as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery()), params)
            rows = session.scalars(
                statement.order_by(*order).limit(limit).offset(offset), params,
            ).all()
            # Keep the category selector complete even when this query returns no skills.
            categories = session.scalars(select(CatalogSkillRecord.category).distinct().order_by(
                CatalogSkillRecord.category
            )).all()
        return Catalog(
            skills=[Skill.model_validate_json(row) for row in rows], categories=categories,
            total=total, has_more=offset + len(rows) < total,
        )

    def keyword_search(
        self, query: str, *, agent: Agent | None = None,
        excluded: Iterable[str] = (), limit: int = 200,
    ) -> list[Skill]:
        match = keyword_query(query)
        if not match:
            return []
        statement = select(CatalogSkillRecord.data).join(
            skills_fts, skills_fts.c.skill_id == CatalogSkillRecord.id,
        ).where(text("skills_fts MATCH :match"))
        excluded = list(excluded)
        if excluded:
            # One JSON parameter avoids SQLite's parameter limit and connection-local temp state.
            exclusions = func.json_each(json.dumps(excluded)).table_valued("value")
            statement = statement.where(CatalogSkillRecord.id.not_in(select(exclusions.c.value)))
        if agent is not None:
            # Unknown compatibility stays eligible; only an explicit list constrains the agent.
            compatibility = func.json_each(CatalogSkillRecord.compatibility).table_valued("value")
            statement = statement.where(or_(
                CatalogSkillRecord.compatibility.is_(None),
                select(compatibility.c.value).where(compatibility.c.value == agent).exists(),
            ))
        with self.db_sessions() as session:
            rows = session.scalars(statement.order_by(
                literal_column("bm25(skills_fts,0,4,1,2,2)"),
                literal_column("catalog_skills.rowid"),
            ).limit(limit), {"match": match}).all()
        return [Skill.model_validate_json(row) for row in rows]

    def eligible(
        self, ids: Iterable[str], *, agent: Agent | None = None, excluded: Iterable[str] = (),
    ) -> list[Skill]:
        # Vector candidates must follow the same eligibility policy as keyword_search.
        excluded = set(excluded)
        return [skill for skill in self.get_many(ids) if skill.id not in excluded and (
            agent is None or skill.compatibility is None or agent in skill.compatibility
        )]
