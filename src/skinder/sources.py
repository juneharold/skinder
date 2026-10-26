"""Read and cache shortlisted skill instructions. Downloaded text is never executed."""

import asyncio
import json
import time
from urllib.parse import quote, unquote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .orm import CatalogSkillRecord, SourceCacheRecord

MAX_FILE_BYTES = 256 * 1024
MAX_EXCERPT = 4000
MAX_PROMPT_CHARS = 200_000
CACHE_SECONDS = 7 * 24 * 60 * 60
MATCH_FIELDS = {
    "id", "name", "description", "category", "requirements", "tags", "workflow",
    "output_style", "evidence", "compatibility", "provenance",
}


def raw_skill_url(skill):
    url = urlsplit(skill.source_url)
    if (url.scheme != "https" or url.hostname != "github.com" or url.port is not None
            or url.username or url.password):
        return None
    parts = unquote(url.path).strip("/").split("/")
    if (len(parts) < 5 or "/".join(parts[:2]).lower() != skill.repository.lower()
            or parts[2] not in {"tree", "blob"}
            or any(part in {"", ".", ".."} or "\\" in part for part in parts)):
        return None
    if parts[2] == "tree" and parts[-1] != "SKILL.md":
        parts.append("SKILL.md")
    if parts[-1] != "SKILL.md":
        return None
    path = "/".join([*parts[:2], *parts[3:]])
    return "https://raw.githubusercontent.com/" + quote(path, safe="/")


def match_metadata(skill):
    data = skill.model_dump(include=MATCH_FIELDS)
    data["description"] = data["description"][:2000]
    data["name"] = data["name"][:300]
    for key in ("requirements", "tags", "workflow", "output_style"):
        data[key] = [item[:500] for item in data[key][:10]]
    data["evidence"] = [
        {"trait": item["trait"][:300], "excerpt": item["excerpt"][:500]}
        for item in data["evidence"][:10]
    ]
    return data


def bounded_request(task, profile, feedback, candidates):
    request = {
        "task": task, "profile": profile, "recent_feedback": list(feedback),
        "eligible_skills": [dict(item) for item in candidates[:40]],
    }
    while len(json.dumps(request, ensure_ascii=False)) > MAX_PROMPT_CHARS:
        with_source = [item for item in request["eligible_skills"] if item.get("source_excerpt")]
        if with_source:
            longest = max(with_source, key=lambda item: len(item["source_excerpt"]))
            longest["source_excerpt"] = ""
            longest["source_truncated"] = True
        elif request["recent_feedback"]:
            request["recent_feedback"].pop()
        elif request["eligible_skills"]:
            request["eligible_skills"].pop()
        else:
            raise ValueError("Task and preferences exceed the matching input limit.")
    return request


class SourceEnricher:
    def __init__(self, catalog, *, transport=None, timeout=20):
        self.catalog = catalog
        self.transport = transport
        self.timeout = timeout

    def cached(self, skill):
        with self.catalog.db_sessions() as session:
            row = session.get(SourceCacheRecord, skill.id)
            current = session.scalar(select(CatalogSkillRecord.source_updated_at).where(
                CatalogSkillRecord.id == skill.id
            ))
            cached = ({
                "skill_id": row.skill_id, "source_url": row.source_url,
                "source_updated_at": row.source_updated_at,
                "fetched_at": row.fetched_at, "content": row.content,
            } if row else None)
        return cached, current

    @staticmethod
    def with_content(item, row, status):
        return {
            **item, "source_status": status, "source_excerpt": row["content"][:MAX_EXCERPT],
            "source_truncated": len(row["content"]) > MAX_EXCERPT,
            "source_fetched_at": row["fetched_at"],
        }

    async def enrich(self, skills):
        results = [match_metadata(skill) for skill in skills]
        semaphore = asyncio.Semaphore(4)
        async with httpx.AsyncClient(
            timeout=10, transport=self.transport, follow_redirects=False,
            headers={"User-Agent": "Skinder-source-reader/1.0"},
        ) as client:
            async def read(index, skill):
                item = results[index]
                if skill.provenance == "bundled":
                    # Bundled metadata already contains reviewed, pinned source evidence.
                    results[index] = {**item, "source_status": "reviewed-metadata"}
                    return
                cached, stamp = self.cached(skill)
                same_source = cached and cached["source_url"] == skill.source_url
                fresh = same_source and cached["source_updated_at"] == stamp and (
                    time.time() - cached["fetched_at"] < CACHE_SECONDS
                )
                if fresh:
                    results[index] = self.with_content(item, cached, "cached")
                    return
                results[index] = (
                    self.with_content(item, cached, "stale-cache") if same_source
                    else {**item, "source_status": "unavailable"}
                )
                try:
                    url = raw_skill_url(skill)
                    if not url:
                        return
                    async with semaphore, client.stream("GET", url) as response:
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_FILE_BYTES:
                                return
                        text = content.decode("utf-8-sig")
                    if not text.strip():
                        return
                except (httpx.HTTPError, UnicodeError, ValueError):
                    return
                fetched_at = time.time()
                with self.catalog.db_sessions.begin() as session:
                    statement = insert(SourceCacheRecord).values(
                        skill_id=skill.id, source_url=skill.source_url,
                        source_updated_at=stamp, fetched_at=fetched_at, content=text,
                    )
                    session.execute(statement.on_conflict_do_update(
                        index_elements=["skill_id"],
                        set_={key: getattr(statement.excluded, key) for key in (
                            "source_url", "source_updated_at", "fetched_at", "content",
                        )},
                    ))
                results[index] = self.with_content(
                    item, {"content": text, "fetched_at": fetched_at}, "fetched"
                )

            try:
                async with asyncio.timeout(self.timeout):
                    await asyncio.gather(*(read(index, skill) for index, skill in enumerate(skills)))
            except TimeoutError:
                pass
        return results
