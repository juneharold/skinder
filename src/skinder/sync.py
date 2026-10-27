"""Resumable SkillsMP keyword searches; coverage is limited to configured keywords."""

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
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from sqlalchemy import Integer, cast, create_engine, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.pool import NullPool

from .catalog import CatalogStore, stable_id
from .database import CATALOG_MAINTENANCE_LOCK, Database
from .diagnostics import configure_logging
from .models import InstallCommands, Skill
from .orm import CatalogAliasRecord, SyncPartitionRecord, SyncRunRecord
from .settings import Settings, load_settings

BASE = "https://skillsmp.com"
SEARCH_PATH = "/api/v1/skills/search"
STATE_VERSION = 2
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
        "coverage": "best-effort within configured keywords; not a full-catalog inventory",
    }


class Paused(Exception):
    pass


class BudgetReached(Paused):
    pass


class SearchUncertain(Paused):
    pass


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
        elif not path or not (path == directory or path.startswith(directory + "/")):
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
    route = row.get("skillUrl") or row.get("route")
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
        self, catalog: CatalogStore, client: httpx.Client, *, settings: Settings | None = None,
        max_requests: int | None = None, sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.catalog, self.client = catalog, client
        self.settings = settings or catalog.settings
        self.max_requests = max_requests if max_requests is not None else self.settings.sync_max_requests
        self.sleep, self.monotonic = sleep, monotonic
        self.calls, self.last_call = 0, None
        with catalog.db_sessions.begin() as session:
            row = session.get(SyncRunRecord, 1)
            self.state = dict(row.data) if row else {}
            # Replace obsolete crawl checkpoints, preserving inventory and request accounting.
            if self.state.get("version") != STATE_VERSION:
                session.execute(delete(SyncPartitionRecord))
                self.state = {key: value for key, value in self.state.items() if key in (
                    "budget_date", "daily_requests", "provider_daily_remaining", "retry_after",
                    "last_request_at",
                )}
            if self.state.get("status") in (None, "completed"):
                self.state.update(
                    version=STATE_VERSION, status="running", started_at=now(), updated_at=now(),
                    requests=0, added=0, updated=0, duplicates=0, invalid=0,
                    warnings=[], last_error=None,
                )
                # A new scan refreshes bounded windows; uncertain charges remain blocked.
                session.execute(delete(SyncPartitionRecord).where(
                    SyncPartitionRecord.status != "pending",
                    SyncPartitionRecord.data["retry_blocked"].as_boolean().is_not(True),
                ))
            self.state["keywords"] = self.settings.sync_keywords
            session.execute(delete(SyncPartitionRecord).where(
                SyncPartitionRecord.data["query"].as_string().not_in(self.settings.sync_keywords),
            ))
            for query in self.settings.sync_keywords:
                session.execute(insert(SyncPartitionRecord).values(
                    key=query, status="pending",
                    data={"query": query, "page": 1, "fingerprints": [], "capped": False},
                ).on_conflict_do_nothing(index_elements=["key"]))
        self.save()

    def save(self):
        self.state["updated_at"] = now()
        with self.catalog.db_sessions.begin() as session:
            session.merge(SyncRunRecord(id=1, data=dict(self.state)))

    def checkpoint(self, key, data, status="pending"):
        # Upserts precede the checkpoint; interrupted page imports are idempotent.
        with self.catalog.db_sessions.begin() as session:
            session.execute(update(SyncPartitionRecord).where(
                SyncPartitionRecord.key == key
            ).values(status=status, data=data))
        self.save()

    def request(self, params):
        for attempt in range(4):
            if self.calls >= self.max_requests:
                raise BudgetReached("Request budget reached; rerun to resume")
            today = datetime.fromtimestamp(time.time(), UTC).date().isoformat()
            if self.state.get("budget_date") != today:
                self.state.update(budget_date=today, daily_requests=0)
                self.state.pop("provider_daily_remaining", None)
            if (self.state["daily_requests"] >= self.settings.sync_daily_requests
                    or self.state.get("provider_daily_remaining") == 0):
                raise BudgetReached("Daily request budget exhausted; resume after midnight UTC")
            delay = max(2 ** attempt if attempt else 0,
                        self.state.get("retry_after", 0) - time.time(),
                        self.settings.sync_min_interval_seconds
                        - (time.time() - self.state.get("last_request_at", 0)))
            if self.last_call is not None:
                delay = max(delay, self.settings.sync_min_interval_seconds
                            - (self.monotonic() - self.last_call))
            # Long provider cooldowns belong to a later scheduled invocation.
            if delay > 60:
                raise Paused("Provider cooldown active; resume after retry_after")
            if delay:
                self.sleep(delay)
            self.calls += 1
            self.state["requests"] += 1
            self.state["daily_requests"] += 1
            if self.state.get("provider_daily_remaining") is not None:
                self.state["provider_daily_remaining"] -= 1
            self.state["last_request_at"] = time.time()
            # Reserve the request durably before sending, including retries and interruptions.
            self.save()
            self.last_call = self.monotonic()
            try:
                response = self.client.get(BASE + SEARCH_PATH, params=params)
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
                    except (ValueError, TypeError, OverflowError):
                        delay = 0
                self.state["retry_after"] = max(self.state.get("retry_after", 0),
                                                time.time() + max(0, delay))
            remaining = response.headers.get("X-RateLimit-Daily-Remaining")
            if remaining is not None and remaining.isdigit():
                self.state["provider_daily_remaining"] = int(remaining)
            self.save()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code == "SEARCH_COMPLETION_UNCERTAIN":
                raise SearchUncertain("SEARCH_COMPLETION_UNCERTAIN: review provider usage before retrying")
            if code == "DAILY_QUOTA_EXCEEDED":
                self.state["provider_daily_remaining"] = 0
                self.save()
                raise Paused("Provider daily quota exhausted; resume after midnight UTC")
            if response.status_code in (401, 403):
                # Persist a cooldown so scheduled restarts cannot hammer an access failure.
                midnight = (int(time.time()) // 86400 + 1) * 86400
                self.state["retry_after"] = max(self.state.get("retry_after", 0), midnight)
                self.save()
                raise Paused(f"Provider access response HTTP {response.status_code}")
            if response.status_code >= 500 or response.status_code in (408, 429):
                if attempt == 3:
                    raise Paused(f"Provider retries exhausted: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise Paused(f"Unexpected provider response HTTP {response.status_code}")
            return response

    def page(self, key, data):
        try:
            response = self.request({"q": data["query"], "page": data["page"],
                                     "limit": 50, "sortBy": "stars"})
        except SearchUncertain as exc:
            data["retry_blocked"] = True
            data["error"] = str(exc)
            self.checkpoint(key, data, "unresolved")
            raise
        try:
            payload = response.json()
            if payload["success"] is not True:
                raise ValueError("Unsuccessful search")
            rows, pagination = payload["data"]["skills"], payload["data"]["pagination"]
            if not isinstance(rows, list) or not isinstance(pagination, dict):
                raise TypeError("Wrong container types")
            page, has_next = pagination["page"], pagination["hasNext"]
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
        identifiers = sorted(str(row.get("id")) for row in rows if isinstance(row, dict))
        fingerprint = hashlib.sha256(json.dumps(identifiers or rows, sort_keys=True).encode()).hexdigest()
        if page != data["page"] or fingerprint in data["fingerprints"] or (has_next and not rows):
            data["error"] = "Provider repeated a page or stopped advancing"
            self.checkpoint(key, data, "unresolved")
            return
        for row in rows:
            try:
                skill = import_skill(row)
                previous = self.catalog.get(skill.id)
                if previous:
                    skill = import_skill(row, previous=previous)
                outcome = self.catalog.upsert(skill, provider_id=row["id"], raw=row,
                                              source_updated_at=row.get("updatedAt"))
                self.state["duplicates" if outcome == "unchanged" else outcome] += 1
            except (ValueError, TypeError, KeyError):
                self.state["invalid"] += 1
        data["fingerprints"].append(fingerprint)
        data["page"] += 1
        # Estimated totals/totalPages are not stopping conditions; follow explicit hasNext.
        if has_next:
            status = "pending"
        elif data["capped"]:
            status = "unresolved"
        else:
            status = "completed"
        self.checkpoint(key, data, status)
        logger.info("SkillsMP query %r page %s: %s rows; added %s, updated %s, duplicates %s",
                    data["query"], page, len(rows), self.state["added"],
                    self.state["updated"], self.state["duplicates"])

    def run(self):
        self.state.update(status="running", last_error=None, pause_reason=None)
        self.save()
        try:
            while True:
                with self.catalog.db_sessions() as session:
                    partition = session.execute(select(
                        SyncPartitionRecord.key, SyncPartitionRecord.data,
                    ).where(SyncPartitionRecord.status == "pending").order_by(
                        SyncPartitionRecord.data["page"].as_integer(), SyncPartitionRecord.ordinal,
                    ).limit(1)).first()
                if not partition:
                    break
                self.page(partition.key, partition.data)
            self.state["status"] = "completed"
        except Paused as exc:
            self.state.update(status="paused", last_error=str(exc),
                              pause_reason="budget" if isinstance(exc, BudgetReached) else "provider")
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
    settings = load_settings("ingest")
    configure_logging(settings.log_level)
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
                if (result.get("sync", {}).get("status") == "paused"
                        and result["sync"].get("pause_reason") != "budget"):
                    raise SystemExit(2)
        except RuntimeError as exc:
            parser.exit(1, str(exc) + "\n")
    finally:
        database.close()


if __name__ == "__main__":
    main()
