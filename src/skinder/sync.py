"""Resumable, best-effort SkillsMP metadata discovery; never a completeness proof."""

import argparse
import fcntl
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from .catalog import CatalogStore, default_catalog_path, stable_id
from .models import InstallCommands, Skill

BASE = "https://skillsmp.com"
TAXONOMY_LIMIT = 1000
DIMENSIONS = ("language", "category", "occupation")


def now():
    return datetime.now(UTC).isoformat()


def sync_status(catalog):
    """Read existing state without initializing or mutating sync tables."""
    with catalog.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sync_run'").fetchone():
            return None
        row = db.execute("SELECT data FROM sync_run WHERE id=1").fetchone()
        if not row:
            return None
        state = json.loads(row[0])
        rows = db.execute("SELECT status,count(*) AS n FROM sync_partitions GROUP BY status").fetchall()
        diagnostics = [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM sync_partitions WHERE status='unresolved' "
            "OR json_extract(data,'$.capped')=1 OR json_extract(data,'$.error') IS NOT NULL "
            "ORDER BY rowid LIMIT 50"
        )]
        capped, failed = db.execute(
            "SELECT coalesce(sum(json_extract(data,'$.capped')),0),"
            "coalesce(sum(json_extract(data,'$.error') IS NOT NULL),0) FROM sync_partitions"
        ).fetchone()
    counts = {key: 0 for key in ("pending", "completed", "unresolved", "failed", "capped")}
    counts.update((row["status"], row["n"]) for row in rows)
    counts.update(capped=capped, failed=failed)
    return {key: value for key, value in state.items() if key not in (
        "discovery_pending", "discovery_seen", "discovered",
    )} | {
        "partitions": counts,
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
        if tag == "a":
            href = dict(attrs).get("href", "")
            url = urlsplit(urljoin(BASE, href))
            if url.scheme == "https" and url.netloc == "skillsmp.com":
                path = url.path.rstrip("/")
                if re.fullmatch(r"/(categories|occupations)/[a-zA-Z0-9_/-]+", path):
                    self.links.add(path)


def language_values(document):
    """Find language enums in parameters or named schemas in the public API contract."""
    result = set()

    def visit(value, language=False):
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


def import_skill(row, category=None, previous=None):
    if not isinstance(row, dict):
        raise TypeError("Skill row is not an object")
    for field in ("id", "name", "githubUrl"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"Missing {field}")
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
        if branch and tail.startswith(branch + "/"):
            directory = tail[len(branch) + 1:]
        else:
            branch, directory = parts[3], "/".join(parts[4:])
        if parts[2] == "blob":
            path = directory
        elif path == directory or path.startswith(directory + "/"):
            pass
        else:
            path = (directory + "/" if directory else "") + (path or "SKILL.md")
    if not branch or not path or any(x in ("", ".", "..") for x in path.split("/")):
        raise ValueError("Missing or invalid source branch/path")
    if not path.endswith("/SKILL.md") and path != "SKILL.md":
        path += "/SKILL.md"
    identifier = stable_id(repository, path)
    tags = set(previous.tags if previous else [])
    if category:
        tags.add(category)
    route = row.get("route")
    provider_url = urljoin(BASE, route) if isinstance(route, str) else None
    if provider_url and urlsplit(provider_url).netloc != "skillsmp.com":
        provider_url = None
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
    def __init__(self, catalog, client, *, max_requests=None, sleep=time.sleep, monotonic=time.monotonic):
        self.catalog, self.client = catalog, client
        self.max_requests, self.sleep, self.monotonic = max_requests, sleep, monotonic
        self.calls, self.last_call = 0, None
        with catalog.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sync_run (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_partitions (
                    key TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL
                );
            """)
            row = db.execute("SELECT data FROM sync_run WHERE id=1").fetchone()
            self.state = json.loads(row[0]) if row else None
            if not self.state or self.state["status"] == "completed":
                db.execute("DELETE FROM sync_partitions")
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
        with self.catalog.connect() as db:
            db.execute("INSERT OR REPLACE INTO sync_run VALUES (1,?)", (json.dumps(self.state),))

    def enqueue(self, filters, sort="stars"):
        key = json.dumps({"filters": filters, "sort": sort}, sort_keys=True)
        data = {"filters": filters, "sort": sort, "page": 1, "fingerprints": [], "capped": False}
        with self.catalog.connect() as db:
            db.execute("INSERT OR IGNORE INTO sync_partitions VALUES (?, 'pending', ?)",
                       (key, json.dumps(data)))

    def checkpoint(self, key, data, status="pending"):
        with self.catalog.connect() as db:
            db.execute("UPDATE sync_partitions SET status=?,data=? WHERE key=?",
                       (status, json.dumps(data), key))
        self.save()

    def request(self, path, params=None):
        for attempt in range(4):
            if self.max_requests is not None and self.calls >= self.max_requests:
                raise Paused("Request budget reached; rerun to resume")
            delay = max(0, self.state.get("retry_after", 0) - time.time())
            if self.last_call is not None:
                delay = max(delay, 2 - (self.monotonic() - self.last_call))
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
                    raise Paused(f"Transport failure: {exc}") from exc
                self.sleep(2 ** (attempt + 1))
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
            if response.status_code in (401, 403, 429):
                raise Paused(f"Provider access/quota response HTTP {response.status_code}")
            if response.status_code >= 500 or response.status_code == 408:
                if attempt == 3:
                    raise Paused(f"Provider unavailable: HTTP {response.status_code}")
                self.sleep(max(2 ** (attempt + 1), self.state.get("retry_after", 0) - time.time()))
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
            if not parser.links and path in ("/categories", "/occupations"):
                raise Paused(f"No taxonomy links found on {path}; provider format/access may have changed")
            for link in sorted(parser.links):
                kind, value = link.strip("/").split("/", 1)
                self.add_filter("category" if kind == "categories" else "occupation", value)
                if link not in self.state["discovery_seen"] and link not in self.state["discovery_pending"]:
                    if len(self.state["discovery_seen"]) + len(self.state["discovery_pending"]) < TAXONOMY_LIMIT:
                        self.state["discovery_pending"].append(link)
                    else:
                        self.warn(f"Taxonomy discovery bounded at {TAXONOMY_LIMIT} pages; additional pages omitted")
            if not parser.links:
                self.warn(f"No additional taxonomy links found on {path}")
        self.state["discovery_pending"].pop(0)
        self.state["discovery_seen"].append(path)
        self.save()

    def add_filter(self, kind, value):
        if value not in self.state["discovered"][kind]:
            self.state["discovered"][kind].append(value)
            self.enqueue({kind: value})
            # Newly found filters must also refine capped groups discovered earlier.
            with self.catalog.connect() as db:
                partitions = db.execute("SELECT data FROM sync_partitions").fetchall()
            for row in partitions:
                data = json.loads(row[0])
                if data["capped"] and data["sort"] == "stars":
                    self.expand(data)

    def warn(self, message):
        if message not in self.state["warnings"]:
            self.state["warnings"].append(message)

    def expand(self, data):
        self.enqueue(data["filters"], "recent")
        for dimension in DIMENSIONS:
            if dimension not in data["filters"]:
                for value in self.state["discovered"][dimension]:
                    self.enqueue(data["filters"] | {dimension: value})
                return

    def page(self, key, data):
        try:
            response = self.request("/api/skills", {
                **data["filters"], "page": data["page"], "limit": 100, "sortBy": data["sort"],
            })
        except Paused as exc:
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
        identifiers = sorted(str(row.get("id")) for row in rows if isinstance(row, dict))
        if identifiers:
            fingerprint = hashlib.sha256(json.dumps(identifiers).encode()).hexdigest()
        if page != data["page"] or fingerprint in data["fingerprints"] or (has_next and not rows):
            data["error"] = "Provider repeated a page or stopped advancing"
            if data["capped"]:
                self.expand(data)
            self.checkpoint(key, data, "unresolved")
            return
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
        terminal_cap = data["capped"] and all(x in data["filters"] for x in DIMENSIONS)
        status = "pending" if has_next else "unresolved" if terminal_cap else "completed"
        self.checkpoint(key, data, status)

    def run(self):
        self.state.update(status="running", last_error=None)
        self.save()
        try:
            delay = max(0, self.state.get("retry_after", 0) - time.time())
            if delay:
                self.sleep(delay)
            # Discover the three public roots first; then alternate discovery and ingestion.
            while any(path not in self.state["discovery_seen"] for path in (
                "/categories", "/occupations", "/openapi.json",
            )):
                self.discover()
            while True:
                with self.catalog.connect() as db:
                    partition = db.execute("SELECT key,data FROM sync_partitions WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
                if partition:
                    self.page(partition["key"], json.loads(partition["data"]))
                if self.state["discovery_pending"]:
                    self.discover()
                elif not partition:
                    break
            self.state["status"] = "completed"
        except (Paused, KeyboardInterrupt) as exc:
            self.state.update(status="paused", last_error=str(exc) or "Interrupted; rerun to resume")
        finally:
            self.save()
        return sync_status(self.catalog)


def positive(value):
    number = int(value)
    if not 1 <= number <= 10_000_000:
        raise argparse.ArgumentTypeError("must be between 1 and 10000000")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default=str(default_catalog_path()))
    parser.add_argument("--max-requests", type=positive, help="remote-call budget for this invocation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--index-only", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    catalog = CatalogStore(args.db)
    if args.status:
        print(json.dumps(sync_status(catalog), indent=2))
        return
    lock_path = catalog.path.with_name(catalog.path.name + ".sync.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another catalog sync/index writer is active.\n")
        catalog.seed()
        result = {}
        if not args.index_only:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                result["sync"] = Sync(catalog, client, max_requests=args.max_requests).run()
        from .retrieval import index_catalog
        result["index"] = index_catalog(catalog, allow_download=True)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
