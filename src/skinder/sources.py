"""Read and cache shortlisted skill instructions. Downloaded text is never executed."""

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict
from urllib.parse import quote, unquote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .catalog import CatalogStore
from .models import Skill
from .orm import CatalogSkillRecord, SourceCacheRecord
from .settings import Settings
from .work import run_sync

MAX_FILE_BYTES = 256 * 1024
# Cached downloads may be larger than the excerpts included in matching requests.
MAX_EXCERPT = 4000
MAX_PROMPT_BYTES = 65536
CACHE_SECONDS = 7 * 24 * 60 * 60
MATCH_FIELDS = {
    "id", "name", "description", "category", "requirements", "tags", "workflow",
    "output_style", "evidence", "compatibility", "provenance",
}


class SourceContent(TypedDict):
    content: str
    fetched_at: float


class CachedSource(SourceContent):
    skill_id: str
    source_url: str
    source_updated_at: str | None


def raw_skill_url(skill: Skill) -> str | None:
    # Derive downloads from repository-matched GitHub paths, never arbitrary provider URLs.
    url = urlsplit(skill.source_url)
    if (url.scheme != "https" or url.hostname != "github.com" or url.port is not None
            or url.username or url.password):
        return None
    # Check decoded segments so encoded traversal or separators cannot bypass the path checks.
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


def match_metadata(skill: Skill) -> dict[str, Any]:
    # Per-field limits leave room for evidence from other candidates in the request.
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


def bounded_request(
    task: str, profile: dict[str, Any], feedback: Sequence[Mapping[str, Any]],
    candidates: Sequence[dict[str, Any]], settings: Settings | None = None,
    *, request_size: Callable[[dict[str, Any]], int] | None = None,
) -> dict[str, Any]:
    settings = settings or Settings(_env_file=None)
    request = {
        "task": task, "profile": profile, "recent_feedback": list(feedback),
        "eligible_skills": [dict(item) for item in candidates[:40]],
    }
    # The provider can measure its complete envelope, including the second JSON escaping
    # of message content. Standalone callers retain the plain input byte limit.
    measure = request_size or (lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8")))
    # Trim source text, then oldest feedback, then lowest-ranked candidates; keep task/preferences.
    while measure(request) > settings.max_prompt_bytes:
        with_source = [item for item in request["eligible_skills"] if item.get("source_excerpt")]
        if with_source:
            longest = max(with_source, key=lambda item: len(item["source_excerpt"]))
            longest["source_excerpt"] = ""
            longest["source_truncated"] = True
            continue
        if request["recent_feedback"]:
            request["recent_feedback"].pop()
            continue
        if request["eligible_skills"]:
            request["eligible_skills"].pop()
            continue
        raise ValueError("Task and preferences exceed the matching input limit.")
    return request


class SourceEnricher:
    def __init__(
        self, catalog: CatalogStore, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        self.catalog = catalog
        self.transport = transport
        self.settings = settings or catalog.settings
        self.timeout = timeout or self.settings.source_enrichment_timeout_seconds
        # One enricher is shared by all jobs in a worker.
        self.semaphore = asyncio.Semaphore(self.settings.source_concurrency)

    def cached(self, skill: Skill) -> tuple[CachedSource | None, str | None]:
        with self.catalog.db_sessions() as session:
            row = session.get(SourceCacheRecord, skill.id)
            # Provider update timestamps live on catalog rows, outside the public Skill model.
            current = session.scalar(select(CatalogSkillRecord.source_updated_at).where(
                CatalogSkillRecord.id == skill.id
            ))
            cached: CachedSource | None = ({
                "skill_id": row.skill_id, "source_url": row.source_url,
                "source_updated_at": row.source_updated_at,
                "fetched_at": row.fetched_at, "content": row.content,
            } if row else None)
        return cached, current

    def _cache(
        self, skill: Skill, source_updated_at: str | None, fetched_at: float, content: str,
    ) -> None:
        # If the provider stamp changes during a download, the saved old stamp forces a refresh.
        with self.catalog.db_sessions.begin() as session:
            statement = insert(SourceCacheRecord).values(
                skill_id=skill.id, source_url=skill.source_url,
                source_updated_at=source_updated_at, fetched_at=fetched_at, content=content,
            )
            session.execute(statement.on_conflict_do_update(
                index_elements=["skill_id"],
                set_={key: getattr(statement.excluded, key) for key in (
                    "source_url", "source_updated_at", "fetched_at", "content",
                )},
            ))

    def with_content(
        self, item: dict[str, Any], row: SourceContent, status: str,
    ) -> dict[str, Any]:
        # Downloaded instructions remain reference text; they do not become reviewed metadata.
        return {
            **item, "source_status": status, "source_excerpt": row["content"][:self.settings.source_excerpt_chars],
            "source_truncated": len(row["content"]) > self.settings.source_excerpt_chars,
            "source_fetched_at": row["fetched_at"],
        }

    async def enrich(self, skills: Sequence[Skill]) -> list[dict[str, Any]]:
        results = [match_metadata(skill) for skill in skills]
        # A redirect must not move an otherwise allowed download to another host.
        async with httpx.AsyncClient(
            timeout=self.settings.source_http_timeout_seconds, transport=self.transport, follow_redirects=False,
            headers={"User-Agent": "Skinder-source-reader/1.0"},
        ) as client:
            async def read(index: int, skill: Skill) -> None:
                item = results[index]
                if skill.provenance == "bundled":
                    # Bundled metadata already contains reviewed, pinned source evidence.
                    results[index] = {**item, "source_status": "reviewed-metadata"}
                    return
                cached, stamp = await run_sync(self.cached, skill)
                same_source = cached and cached["source_url"] == skill.source_url
                # Age still forces refreshes when the provider supplies no update timestamp.
                fresh = same_source and cached["source_updated_at"] == stamp and (
                    time.time() - cached["fetched_at"] < self.settings.source_cache_ttl_seconds
                )
                if fresh:
                    results[index] = self.with_content(item, cached, "cached")
                    return
                # Set fallback before downloading so failures and deadlines retain usable evidence.
                # Reuse stale text only when it still describes the same source URL.
                results[index] = (
                    self.with_content(item, cached, "stale-cache") if same_source
                    else {**item, "source_status": "unavailable"}
                )
                try:
                    url = raw_skill_url(skill)
                    if not url:
                        return
                    async with self.semaphore, client.stream("GET", url) as response:
                        response.raise_for_status()
                        content = bytearray()
                        # Reject oversized instructions without buffering the entire response.
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > self.settings.source_max_bytes:
                                return
                        text = content.decode("utf-8-sig")
                    if not text.strip():
                        return
                except (httpx.HTTPError, UnicodeError, ValueError):
                    return
                fetched_at = time.time()
                # Database waits stay off the event loop; run_sync drains writes on cancellation.
                await run_sync(self._cache, skill, stamp, fetched_at, text)
                results[index] = self.with_content(
                    item, {"content": text, "fetched_at": fetched_at}, "fetched"
                )

            try:
                async with asyncio.timeout(self.timeout):
                    async with asyncio.TaskGroup() as reads:
                        for index, skill in enumerate(skills):
                            reads.create_task(read(index, skill))
            except TimeoutError:
                # One slow source must not discard evidence that other reads already obtained.
                pass
        return results
