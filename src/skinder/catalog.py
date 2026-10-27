"""PostgreSQL catalog and literal keyword retrieval, independent of task history."""

import hashlib
import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy import Text, all_, any_, bindparam, func, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY, insert

from .database import Database
from .models import Agent, Catalog, Skill
from .orm import CatalogAliasRecord, CatalogSkillRecord
from .settings import Settings


def stable_id(repository: str, skill_path: str) -> str:
    directory = str(PurePosixPath(skill_path).parent)
    return hashlib.sha256(f"{repository.lower()}:{directory}".encode()).hexdigest()[:20]


def retrieval_text(skill: Skill) -> str:
    return "\n".join([
        skill.name, skill.description, skill.category, " ".join(skill.tags),
        *skill.requirements, *skill.workflow, *skill.output_style,
    ])


def keyword_query(query: str) -> str:
    # Only Unicode letters/numbers reach to_tsquery; user operators are always literal words.
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))[:64]
    return " | ".join(f"'{token}'" for token in tokens)


def eligibility(statement, agent: Agent | None, excluded: Iterable[str]):
    excluded = list(set(excluded))
    if excluded:
        statement = statement.where(CatalogSkillRecord.id != all_(
            bindparam("excluded", excluded, type_=ARRAY(Text))))
    if agent is not None:
        statement = statement.where(or_(CatalogSkillRecord.compatibility.is_(None),
                                        agent == any_(CatalogSkillRecord.compatibility)))
    return statement


class CatalogStore:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or Settings(_env_file=None)
        self.engine = database.engine
        self.db_sessions = database.db_sessions

    def close(self) -> None:
        self.database.close()

    def upsert(
        self, skill: Skill, *, provider_id: str | None = None,
        raw: dict[str, Any] | None = None, source_updated_at: str | float | None = None,
    ) -> Literal["added", "updated", "unchanged"]:
        content = retrieval_text(skill)
        payload = skill.model_dump(mode="json", by_alias=True)
        source_updated_at = str(source_updated_at) if source_updated_at is not None else None
        with self.db_sessions.begin() as session:
            # A transaction lock covers absent rows too, preserving precedence under concurrent imports.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 73519))"),
                            {"id": skill.id})
            previous = session.get(CatalogSkillRecord, skill.id)
            if provider_id is not None:
                statement = insert(CatalogAliasRecord).values(
                    provider="skillsmp", external_id=provider_id, skill_id=skill.id, payload=raw)
                session.execute(statement.on_conflict_do_update(
                    index_elements=["provider", "external_id"],
                    set_={"skill_id": statement.excluded.skill_id, "payload": statement.excluded.payload}))
            if previous and previous.provenance == "bundled" and skill.provenance != "bundled":
                return "unchanged"
            if previous and previous.data == payload and previous.source_updated_at == source_updated_at:
                return "unchanged"
            values = dict(id=skill.id, data=payload, provenance=skill.provenance, name=skill.name,
                          description=skill.description, tags=" ".join(skill.tags), author=skill.author,
                          category=skill.category, compatibility=skill.compatibility,
                          retrieval_text=content, text_hash=hashlib.sha256(content.encode()).hexdigest(),
                          source_updated_at=source_updated_at)
            statement = insert(CatalogSkillRecord).values(**values)
            session.execute(statement.on_conflict_do_update(index_elements=["id"],
                set_={key: getattr(statement.excluded, key) for key in values if key != "id"}))
        return "updated" if previous else "added"

    def get(self, skill_id: str) -> Skill | None:
        with self.db_sessions() as session:
            row = session.get(CatalogSkillRecord, skill_id)
            return Skill.model_validate(row.data) if row else None

    def get_many(self, ids: Iterable[str]) -> list[Skill]:
        ids = list(dict.fromkeys(ids))
        found = {}
        with self.db_sessions() as session:
            for start in range(0, len(ids), 500):
                rows = session.execute(select(CatalogSkillRecord.id, CatalogSkillRecord.data).where(
                    CatalogSkillRecord.id.in_(ids[start:start + 500])))
                found.update((row.id, Skill.model_validate(row.data)) for row in rows)
        return [found[skill_id] for skill_id in ids if skill_id in found]

    def count(self) -> int:
        with self.db_sessions() as session:
            return session.scalar(select(func.count()).select_from(CatalogSkillRecord))

    def page(self, *, q: str = "", category: str | None = None,
             offset: int = 0, limit: int = 50) -> Catalog:
        match = keyword_query(q)
        statement = select(CatalogSkillRecord.data)
        order = (func.lower(CatalogSkillRecord.name), CatalogSkillRecord.id)
        if match:
            query = func.to_tsquery("simple", match)
            statement = statement.where(CatalogSkillRecord.search_vector.op("@@")(query))
            order = (func.ts_rank_cd(CatalogSkillRecord.search_vector, query).desc(), *order)
        elif q.strip():
            statement = statement.where(False)
        if category:
            statement = statement.where(CatalogSkillRecord.category == category)
        with self.db_sessions() as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            rows = session.scalars(statement.order_by(*order).limit(limit).offset(offset)).all()
            categories = session.scalars(select(CatalogSkillRecord.category).distinct().order_by(
                CatalogSkillRecord.category)).all()
        return Catalog(skills=[Skill.model_validate(row) for row in rows], categories=categories,
                       total=total, has_more=offset + len(rows) < total)

    def keyword_search(self, query: str, *, agent: Agent | None = None,
                       excluded: Iterable[str] = (), limit: int = 200) -> list[Skill]:
        match = keyword_query(query)
        if not match:
            return []
        query = func.to_tsquery("simple", match)
        statement = eligibility(select(CatalogSkillRecord.data).where(
            CatalogSkillRecord.search_vector.op("@@")(query)), agent, excluded)
        with self.db_sessions() as session:
            rows = session.scalars(statement.order_by(
                func.ts_rank_cd(CatalogSkillRecord.search_vector, query).desc(),
                func.lower(CatalogSkillRecord.name), CatalogSkillRecord.id).limit(limit)).all()
        return [Skill.model_validate(row) for row in rows]

    def eligible(self, ids: Iterable[str], *, agent: Agent | None = None,
                 excluded: Iterable[str] = ()) -> list[Skill]:
        excluded = set(excluded)
        return [skill for skill in self.get_many(ids) if skill.id not in excluded and (
            agent is None or skill.compatibility is None or agent in skill.compatibility)]
