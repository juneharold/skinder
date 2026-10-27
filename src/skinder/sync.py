"""Resumable, best-effort SkillsMP metadata discovery; never a completeness proof."""

import argparse
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from sqlalchemy import Integer, cast, create_engine, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.pool import NullPool

from .catalog import CatalogStore, stable_id
from .database import CATALOG_MAINTENANCE_LOCK, Database
from .models import InstallCommands, Skill
from .orm import CatalogAliasRecord, SyncPartitionRecord, SyncRunRecord
from .settings import Settings, load_settings

BASE = "https://skillsmp.com"
TAXONOMY_LIMIT = 1000
DIMENSIONS = ("language", "category", "occupation")
logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(UTC).isoformat()


def sync_status(catalog: CatalogStore) -> dict[str, Any] | None:
    """Read existing state without initializing or mutating a sync run."""
    with catalog.db_sessions() as session:
        row = session.get(SyncRunRecord, 1)
        if not row:
            return None
        state = row.data
        # Upserts can commit before run counters are saved; aliases give durable unique inventory.
        imported_unique = session.scalar(select(func.count(
            CatalogAliasRecord.skill_id.distinct()
        )).where(CatalogAliasRecord.provider == "skillsmp"))
        rows = session.execute(select(
            SyncPartitionRecord.status, func.count().label("n"),
        ).group_by(SyncPartitionRecord.status)).all()
        capped_value = SyncPartitionRecord.data["capped"].as_boolean()
        has_error = SyncPartitionRecord.data["error"].as_string().is_not(None)
        diagnostics = list(session.scalars(select(
            SyncPartitionRecord.data,
        ).where(or_(
            SyncPartitionRecord.status == "unresolved", capped_value.is_(True), has_error,
        )).order_by(SyncPartitionRecord.ordinal).limit(50)))
        capped, failed = session.execute(select(
            func.coalesce(func.sum(cast(capped_value, Integer)), 0),
            func.coalesce(func.sum(cast(has_error, Integer)), 0),
        )).one()
    counts = {key: 0 for key in ("pending", "completed", "unresolved", "failed", "capped")}
    counts.update((row.status, row.n) for row in rows)
    # Cap/error counts overlap status counts; adding them together would count partitions twice.
    counts.update(capped=capped, failed=failed)
    return {key: value for key, value in state.items() if key not in (
        "discovery_pending", "discovery_seen", "discovered",
    )} | {
        "partitions": counts,
        "imported_unique_total": imported_unique,
        "counter_scope": "Row counters describe this run and may undercount interrupted writes; "
                         "imported_unique_total is the durable distinct provider inventory across runs",
        "partition_examples": [{key: value for key, value in row.items()
                                if key != "fingerprints"} for row in diagnostics],
        "discovered": {key: len(value) for key, value in state["discovered"].items()},
        "taxonomy_pages_pending": len(state["discovery_pending"]),
        "coverage": "best-effort; provider caps and undiscovered taxonomy prevent a completeness proof",
    }


class Paused(Exception):
    pass


class TaxonomyLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        # General page links must not grow the crawl beyond the provider's taxonomy.
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        url = urlsplit(urljoin(BASE, href))
        if url.scheme != "https" or url.netloc != "skillsmp.com":
            return
        path = url.path.rstrip("/")
        if not re.fullmatch(r"/(categories|occupations)/[a-zA-Z0-9_/-]+", path):
            return
        self.links.add(path)


def language_values(document: Any) -> list[str]:
    """Find language enums in parameters or named schemas in the public API contract."""
    result = set()

    def visit(value, language=False):
        # Language context must survive nested parameter and schema wrappers.
        if isinstance(value, dict):
            language = language or value.get("name") in ("language", "contentLanguage")
            if language and isinstance(value.get("enum"), list):
                result.update(x for x in value["enum"] if isinstance(x, str)
                              and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", x))
            for key, child in value.items():
                visit(child, language or "language" in key.lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, language)

    visit(document)
    return sorted(result)


def import_skill(
    row: Any, category: str | None = None, previous: Skill | None = None,
) -> Skill:
    if not isinstance(row, dict):
        raise TypeError("Skill row is not an object")
    for field in ("id", "name", "githubUrl"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"Missing {field}")
    # These URLs become fetch targets later, so reject alternate hosts and schemes at import.
    url = urlsplit(row["githubUrl"])
    if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
        raise ValueError("Source must be a canonical HTTPS GitHub URL")
    parts = unquote(url.path).strip("/").split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) for x in parts[:2]):
        raise ValueError("Invalid GitHub repository")
    repository = "/".join(parts[:2])
    path = row.get("path") or ""
    branch = row.get("branch")
    if not isinstance(path, str) or (branch is not None and not isinstance(branch, str)):
        raise ValueError("Invalid source path")
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] not in ("tree", "blob"):
            raise ValueError("Missing source directory")
        tail = "/".join(parts[3:])
        # Explicit branch metadata disambiguates branch names that contain slashes.
        if branch and tail.startswith(branch + "/"):
            directory = tail[len(branch) + 1:]
        else:
            branch, directory = parts[3], "/".join(parts[4:])
        if parts[2] == "blob":
            path = directory
        elif not (path == directory or path.startswith(directory + "/")):
            # Providers mix directory-relative and repository-relative paths for the same skill.
            path = (directory + "/" if directory else "") + (path or "SKILL.md")
    if not branch or not path or any(x in ("", ".", "..") for x in path.split("/")):
        raise ValueError("Missing or invalid source branch/path")
    if not path.endswith("/SKILL.md") and path != "SKILL.md":
        path += "/SKILL.md"
    identifier = stable_id(repository, path)
    # Overlapping partitions contribute tags without replacing an established category.
    tags = set(previous.tags if previous else [])
    if category:
        tags.add(category)
    route = row.get("route")
    provider_url = urljoin(BASE, route) if isinstance(route, str) else None
    if provider_url and (urlsplit(provider_url).scheme != "https"
                         or urlsplit(provider_url).netloc != "skillsmp.com"):
        provider_url = None
    # Listings do not establish a pinned revision, license, agent support, or reviewed workflow.
    return Skill(
        id=identifier, name=row["name"].strip(), slug=path.split("/")[-2] if "/" in path else row["name"],
        description=row.get("description", "").strip() if isinstance(row.get("description"), str) else "",
        category=(previous.category if previous and previous.category != "Uncategorized"
                  else category or "Uncategorized"),
        author=row.get("author") if isinstance(row.get("author"), str) else "",
        repository=repository, source_url=f"https://github.com/{repository}/blob/{quote(branch, safe='/')}/{quote(path, safe='/')}",
        revision=None, license=None, compatibility=None, requirements=[], tags=sorted(tags),
        workflow=[], output_style=[], evidence=[], install_commands=InstallCommands(),
        provenance="skillsmp", provider_url=provider_url,
        content_language=row.get("contentLanguage") if isinstance(row.get("contentLanguage"), str) else None,
    )


class Sync:
    def __init__(
        self, catalog: CatalogStore, client: httpx.Client, *, settings: Settings | None = None, max_requests: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.catalog, self.client = catalog, client
        self.settings = settings or catalog.settings
        self.max_requests = max_requests if max_requests is not None else self.settings.sync_max_requests
        self.sleep, self.monotonic = sleep, monotonic
        self.calls, self.last_call = 0, None
        with catalog.db_sessions.begin() as session:
            row = session.get(SyncRunRecord, 1)
            self.state = row.data if row else None
            # Completed runs recrawl; paused runs retain their frontier without clearing inventory.
            if not self.state or self.state["status"] == "completed":
                session.execute(delete(SyncPartitionRecord))
                self.state = {
                    "status": "running", "started_at": now(), "updated_at": now(),
                    "requests": 0, "added": 0, "updated": 0, "duplicates": 0, "invalid": 0,
                    "discovery_pending": ["/categories", "/occupations", "/openapi.json"],
                    "discovery_seen": [], "discovered": {x: [] for x in DIMENSIONS},
                    "warnings": [], "last_error": None,
                }
        self.enqueue({})
        self.save()

    def save(self):
        self.state["updated_at"] = now()
        with self.catalog.db_sessions.begin() as session:
            session.merge(SyncRunRecord(id=1, data=dict(self.state)))

    def enqueue(self, filters, sort="stars"):
        # Repeated discovery must not reset a partition's saved page or completion state.
        key = json.dumps({"filters": filters, "sort": sort}, sort_keys=True)
        data = {"filters": filters, "sort": sort, "page": 1, "fingerprints": [], "capped": False}
        with self.catalog.db_sessions.begin() as session:
            session.execute(insert(SyncPartitionRecord).values(
                key=key, status="pending", data=data,
            ).on_conflict_do_nothing(index_elements=["key"]))

    def checkpoint(self, key, data, status="pending"):
        # Upserts precede this checkpoint; interruption can replay a page, so imports are idempotent.
        with self.catalog.db_sessions.begin() as session:
            session.execute(update(SyncPartitionRecord).where(
                SyncPartitionRecord.key == key
            ).values(status=status, data=data))
        self.save()

    def request(self, path, params=None):
        # Discovery and retries consume the same invocation budget as ingestion requests.
        for attempt in range(4):
            if self.max_requests is not None and self.calls >= self.max_requests:
                raise Paused("Request budget reached; rerun to resume")
            # Wait once for the longest of backoff, provider cooldown, and normal pacing.
            delay = max(2 ** attempt if attempt else 0,
                        self.state.get("retry_after", 0) - time.time())
            if self.last_call is not None:
                delay = max(delay, self.settings.sync_min_interval_seconds - (self.monotonic() - self.last_call))
            if attempt:
                logger.info("Retrying SkillsMP %s in %.1fs (retry %s/3)", path, delay, attempt)
            if delay:
                self.sleep(delay)
            self.calls += 1
            self.state["requests"] += 1
            self.save()
            self.last_call = self.monotonic()
            try:
                response = self.client.get(BASE + path, params=params)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise Paused(f"Transport failure: {type(exc).__name__}") from None
                continue
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError):
                        delay = 0
                # Persist an absolute deadline so a paused process also respects Retry-After.
                self.state["retry_after"] = time.time() + max(0, delay)
                self.save()
            if response.status_code in (401, 403):
                # Authentication/authorization failures will not improve with retries.
                raise Paused(f"Provider access/quota response HTTP {response.status_code}")
            if response.status_code >= 500 or response.status_code in (408, 429):
                if attempt == 3:
                    raise Paused(f"Provider retries exhausted: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise Paused(f"Unexpected provider response HTTP {response.status_code} for {path}")
            return response

    def discover(self):
        path = self.state["discovery_pending"][0]
        response = self.request(path)
        if path == "/openapi.json":
            try:
                values = language_values(response.json())
            except (ValueError, TypeError) as exc:
                raise Paused("Malformed OpenAPI document") from exc
            if not values:
                raise Paused("Public OpenAPI has no discoverable language enum")
            for value in values:
                self.add_filter("language", value)
        else:
            parser = TaxonomyLinks()
            parser.feed(response.text)
            # An empty leaf is valid; empty roots could conceal blocked or changed discovery.
            if not parser.links and path in ("/categories", "/occupations"):
                raise Paused(f"No taxonomy links found on {path}; provider format/access may have changed")
            for link in sorted(parser.links):
                kind, value = link.strip("/").split("/", 1)
                # A known or omitted taxonomy page can still contribute a newly found filter.
                self.add_filter("category" if kind == "categories" else "occupation", value)
                if link in self.state["discovery_seen"] or link in self.state["discovery_pending"]:
                    continue
                if len(self.state["discovery_seen"]) + len(self.state["discovery_pending"]) >= TAXONOMY_LIMIT:
                    self.warn(f"Taxonomy discovery bounded at {TAXONOMY_LIMIT} pages; additional pages omitted")
                    continue
                self.state["discovery_pending"].append(link)
            if not parser.links:
                self.warn(f"No additional taxonomy links found on {path}")
        self.state["discovery_pending"].pop(0)
        self.state["discovery_seen"].append(path)
        self.save()

    def add_filter(self, kind, value):
        if value in self.state["discovered"][kind]:
            return
        self.state["discovered"][kind].append(value)
        self.enqueue({kind: value})
        # Newly found filters must also refine capped groups discovered earlier.
        with self.catalog.db_sessions() as session:
            partitions = session.execute(select(
                SyncPartitionRecord.key, SyncPartitionRecord.data,
            ).where(SyncPartitionRecord.data["capped"].as_boolean().is_(True))).all()
        for row in partitions:
            data = row.data
            if not self.expand(data):
                continue
            with self.catalog.db_sessions.begin() as session:
                session.execute(update(SyncPartitionRecord).where(
                    SyncPartitionRecord.key == row.key
                ).values(data=data))

    def warn(self, message):
        if message not in self.state["warnings"]:
            self.state["warnings"].append(message)

    def expand(self, data):
        # Only stars partitions spawn refinements; recent partitions are alternate capped windows.
        if data["sort"] != "stars":
            return False
        changed = False
        if not data.get("recent_enqueued"):
            self.enqueue(data["filters"], "recent")
            data["recent_enqueued"] = changed = True
        for dimension in DIMENSIONS:
            if dimension in data["filters"]:
                continue
            # The saved count belongs to this first missing dimension, even while its list is empty.
            values = self.state["discovered"][dimension]
            count = data.get("expanded_values", 0)
            for value in values[count:]:
                self.enqueue(data["filters"] | {dimension: value})
            data["expanded_values"] = len(values)
            return changed or count != len(values)
        return changed

    def page(self, key, data):
        try:
            response = self.request("/api/skills", {
                # The website listing endpoint permits 48, unlike REST search's limit of 50.
                **data["filters"], "page": data["page"], "limit": 48, "sortBy": data["sort"],
            })
        except Paused as exc:
            # A local request budget is not a provider error and must leave the page retryable.
            if not str(exc).startswith("Request budget reached"):
                data["error"] = str(exc)
                self.checkpoint(key, data)
            raise
        try:
            payload = response.json()
            rows, pagination = payload["skills"], payload["pagination"]
            if not isinstance(rows, list) or not isinstance(pagination, dict):
                raise TypeError("Wrong container types")
            page = pagination["page"]
            has_next = pagination["hasNext"]
            # Python considers bool an int; this API contract requires distinct field types.
            if type(page) is not int or type(has_next) is not bool:
                raise ValueError("Missing page/hasNext contract")
        except (KeyError, ValueError, TypeError) as exc:
            data["error"] = "Malformed provider page"
            self.checkpoint(key, data)
            raise Paused(data["error"]) from exc
        data.pop("error", None)
        data["capped"] = data["capped"] or pagination.get("isCapped") is True
        data["total"] = pagination.get("total")
        data["max_results"] = pagination.get("maxResults")
        if pagination.get("totalIsExact") is not True:
            self.warn("Some provider totals are estimates; exact coverage is unknown")
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        # Sorting IDs detects repeats even if result order or per-row statistics have changed.
        identifiers = sorted(str(row.get("id")) for row in rows if isinstance(row, dict))
        if identifiers:
            fingerprint = hashlib.sha256(json.dumps(identifiers).encode()).hexdigest()
        if page != data["page"] or fingerprint in data["fingerprints"] or (has_next and not rows):
            data["error"] = "Provider repeated a page or stopped advancing"
            if data["capped"]:
                self.expand(data)
            self.checkpoint(key, data, "unresolved")
            return
        # Reject bad rows individually so one malformed listing cannot block later pages.
        for row in rows:
            try:
                skill = import_skill(row, data["filters"].get("category"))
                previous = self.catalog.get(skill.id)
                if previous:
                    skill = import_skill(row, data["filters"].get("category"), previous)
                outcome = self.catalog.upsert(skill, provider_id=row["id"], raw=row,
                                              source_updated_at=row.get("updatedAt"))
                self.state["duplicates" if outcome == "unchanged" else outcome] += 1
            except (ValueError, TypeError, KeyError):
                self.state["invalid"] += 1
        data["fingerprints"].append(fingerprint)
        data["page"] += 1
        if data["capped"]:
            self.expand(data)
        # A fully refined cap remains unresolved even when pagination claims this is the end.
        terminal_cap = data["capped"] and all(x in data["filters"] for x in DIMENSIONS)
        status = "pending" if has_next else "unresolved" if terminal_cap else "completed"
        self.checkpoint(key, data, status)
        logger.info("SkillsMP page %s: %s rows; added %s, updated %s, duplicate observations %s",
                    page, len(rows), self.state["added"], self.state["updated"], self.state["duplicates"])

    def run(self):
        self.state.update(status="running", last_error=None)
        self.save()
        try:
            # Seed every dimension before ingestion so early capped partitions can be refined.
            while any(path not in self.state["discovery_seen"] for path in (
                "/categories", "/occupations", "/openapi.json",
            )):
                self.discover()
            # Alternate both queues so a large taxonomy or catalog cannot starve the other.
            while True:
                with self.catalog.db_sessions() as session:
                    partition = session.execute(select(
                        SyncPartitionRecord.key, SyncPartitionRecord.data,
                    ).where(SyncPartitionRecord.status == "pending").order_by(
                        SyncPartitionRecord.ordinal
                    ).limit(1)).first()
                if partition:
                    self.page(partition.key, partition.data)
                if self.state["discovery_pending"]:
                    self.discover()
                elif not partition:
                    break
            # Drained queues finish a run; unresolved caps still prevent a coverage claim.
            self.state["status"] = "completed"
        except Paused as exc:
            self.state.update(status="paused", last_error=str(exc))
        except KeyboardInterrupt:
            self.state.update(status="paused", last_error="Interrupted; rerun to resume")
            raise
        finally:
            self.save()
        return sync_status(self.catalog)


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 10_000_000:
        raise argparse.ArgumentTypeError("must be between 1 and 10000000")
    return number


@contextmanager
def writer_lock(catalog: CatalogStore):
    # The dedicated connection must not consume a slot needed by the catalog's transaction pool.
    lock_engine = create_engine(catalog.engine.url, poolclass=NullPool, hide_parameters=True,
        connect_args={"connect_timeout": catalog.settings.db_connect_timeout_seconds})
    try:
        with lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": CATALOG_MAINTENANCE_LOCK})
            if not acquired:
                raise RuntimeError("Another catalog sync/index writer is active.")
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CATALOG_MAINTENANCE_LOCK})
    finally:
        lock_engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-requests", type=positive, help="remote-call budget for this invocation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--index-only", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    settings = load_settings("ingest")
    database = Database(settings)
    catalog = CatalogStore(database, settings)
    try:
        database.check_schema()
        if args.status:
            print(json.dumps(sync_status(catalog), indent=2))
            return
        try:
            with writer_lock(catalog):
                result = {}
                if not args.index_only:
                    try:
                        with httpx.Client(timeout=settings.sync_http_timeout_seconds,
                            follow_redirects=False, headers={
                                "User-Agent": "Skinder/0.1 (best-effort SkillsMP catalog sync)",
                            }) as client:
                            result["sync"] = Sync(catalog, client, max_requests=args.max_requests).run()
                    except KeyboardInterrupt:
                        print(json.dumps({"sync": sync_status(catalog)}, indent=2))
                        raise SystemExit(130) from None
                from .retrieval import index_catalog
                result["index"] = index_catalog(catalog, allow_download=False)
                print(json.dumps(result, indent=2))
                if result.get("sync", {}).get("status") == "paused":
                    raise SystemExit(2)
        except RuntimeError as exc:
            parser.exit(1, str(exc) + "\n")
    finally:
        database.close()


if __name__ == "__main__":
    main()
