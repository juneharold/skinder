"""Offline provider-contract tests assert durable catalog and crawl outcomes."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import pairwise

import httpx
import pytest
from sqlalchemy import func, select

from skinder.catalog import CatalogStore, stable_id
from skinder.orm import CatalogAliasRecord, SyncPartitionRecord, SyncRunRecord
from skinder.sync import Sync, TaxonomyLinks, import_skill, sync_status


def skill_row(name="one", **changes):
    return {"id": name, "name": name, "description": "A verified provider description.",
            "githubUrl": f"https://github.com/example/repo/tree/main/skills/{name}",
            "path": "SKILL.md", "branch": "main", "author": "example",
            "contentLanguage": "en", "route": f"/skills/{name}", **changes}


def page(rows, number=1, *, more=False, capped=False, total=1, exact=True):
    return {"skills": rows, "pagination": {"page": number, "hasNext": more,
            "total": total, "totalIsExact": exact, "isCapped": capped,
            "maxResults": 480 if capped else 1200}}


class Provider:
    def __init__(self, skills):
        self.skills = skills
        self.calls = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/openapi.json":
            return httpx.Response(200, json={"parameters": [
                {"name": "language", "schema": {"enum": ["en"]}}]})
        if path.startswith("/categories"):
            return httpx.Response(200, text='<a href="/categories/tools">Tools</a>')
        if path.startswith("/occupations"):
            return httpx.Response(200, text='<a href="/occupations/developer">Developer</a>')
        result = self.skills(request)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)


class Clock:
    def __init__(self):
        self.elapsed = 1_800_000_000.0
        self.sleeps = []

    def time(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


def run(catalog, provider, **kwargs):
    return Sync(catalog, provider.client, sleep=lambda _: None, **kwargs).run()


@pytest.fixture
def catalog(tmp_path):
    return CatalogStore(tmp_path / "catalog.sqlite3")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("skinder.sync.time.time", clock.time)
    return clock


def test_status_read_does_not_create_sync_state(catalog):
    assert sync_status(catalog) is None
    with catalog.db_sessions() as session:
        assert session.get(SyncRunRecord, 1) is None
        assert session.scalar(select(func.count()).select_from(SyncPartitionRecord)) == 0


def test_taxonomy_links_accept_only_provider_taxonomy_anchors():
    parser = TaxonomyLinks()
    parser.feed('''
        <a href="/categories/tools/">Tools</a>
        <a href="https://skillsmp.com/occupations/software/developer">Developer</a>
        <a href="/categories/tools">Duplicate</a>
        <img href="/categories/image">
        <a href="http://skillsmp.com/categories/insecure">Insecure</a>
        <a href="https://other.example/categories/external">External</a>
        <a href="https://skillsmp.com.evil.test/categories/lookalike">Lookalike</a>
        <a href="/skills/other">Skill</a>
        <a href="/categories/">Root</a>
        <a>No link</a>
    ''')

    assert parser.links == {"/categories/tools", "/occupations/software/developer"}


def test_discovery_bounds_unique_pages_but_keeps_all_filters(catalog, monkeypatch):
    from skinder import sync as module

    monkeypatch.setattr(module, "TAXONOMY_LIMIT", 4)
    document = '''
        <a href="/categories/a">A</a>
        <a href="/categories/a/">Duplicate A</a>
        <a href="/categories/b">B</a>
        <a href="/categories/queued">Queued</a>
        <a href="/categories/seen">Seen</a>
    '''
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=document),
    )) as client:
        sync = Sync(catalog, client, sleep=lambda _: None)
        sync.state["discovery_pending"] = ["/categories", "/categories/queued"]
        sync.state["discovery_seen"] = ["/categories/seen"]

        sync.discover()

    with catalog.db_sessions() as session:
        state = json.loads(session.get(SyncRunRecord, 1).data)
        partitions = [json.loads(data) for data in session.scalars(select(SyncPartitionRecord.data))]
    assert state["discovery_pending"] == ["/categories/queued", "/categories/a"]
    assert state["discovery_seen"] == ["/categories/seen", "/categories"]
    assert state["discovered"]["category"] == ["a", "b", "queued", "seen"]
    assert {row["filters"]["category"] for row in partitions if "category" in row["filters"]} == {
        "a", "b", "queued", "seen",
    }
    assert len(partitions) == 5
    assert len(state["warnings"]) == 1


@pytest.mark.parametrize("filters, missing, later", [
    ({}, "language", "category"),
    ({"language": "en"}, "category", "occupation"),
])
def test_expansion_waits_for_first_missing_dimension(catalog, filters, missing, later):
    provider = Provider(lambda _: page([]))
    sync = Sync(catalog, provider.client, sleep=lambda _: None)
    sync.state["discovered"][later] = ["known"]
    data = {"filters": filters, "sort": "stars", "recent_enqueued": True}

    sync.expand(data)

    with catalog.db_sessions() as session:
        queued = [json.loads(row)["filters"]
                  for row in session.scalars(select(SyncPartitionRecord.data))]
    assert queued == [{}]

    sync.state["discovered"][missing] = ["new"]
    sync.expand(data)

    with catalog.db_sessions() as session:
        queued = [json.loads(row)["filters"]
                  for row in session.scalars(select(SyncPartitionRecord.data))]
    assert len(queued) == 2
    assert {} in queued
    assert filters | {missing: "new"} in queued


def test_overlapping_partitions_preserve_id_metadata_and_category(catalog):
    provider = Provider(lambda _: page([skill_row()]))
    first = run(catalog, provider)
    identifier = stable_id("example/repo", "skills/one/SKILL.md")
    skill = catalog.get(identifier)
    assert catalog.count() == 1
    assert skill.category == "tools"
    assert skill.tags == ["tools"]
    assert skill.source_url == "https://github.com/example/repo/blob/main/skills/one/SKILL.md"
    assert skill.revision is None and skill.license is None and skill.compatibility is None
    assert skill.workflow == [] and skill.install_commands.codex is None
    assert first["status"] == "completed"
    assert first["added"] == 1 and first["duplicates"] >= 2
    second = run(catalog, provider)
    assert catalog.count() == 1 and catalog.get(identifier) == skill
    assert second["added"] == 0


@pytest.mark.parametrize("total", [480, 1200])
def test_exact_capped_totals_expand_all_dimensions_and_recent_window(catalog, total):
    provider = Provider(lambda _: page([skill_row()], capped=True, total=total, exact=True))
    status = run(catalog, provider)
    queries = [dict(request.url.params) for request in provider.calls
               if request.url.path == "/api/skills"]
    assert any(all(key in query for key in ("category", "occupation", "language"))
               and query["sortBy"] == "recent" for query in queries)
    assert len({json.dumps(query, sort_keys=True) for query in queries}) == len(queries)
    assert len(queries) <= 16  # The finite filter combinations cannot grow without bound.
    assert status["status"] == "completed"
    assert status["partitions"]["unresolved"] == 2
    assert status["partitions"]["capped"] == len(queries)
    assert catalog.count() == 1


@pytest.mark.parametrize("canonical", [False, True])
def test_repeated_or_canonicalized_pages_stop_without_losing_data(catalog, canonical):
    def skills(request):
        requested = int(request.url.params["page"])
        return page([skill_row()], 1 if canonical else requested, more=True)

    provider = Provider(skills)
    status = run(catalog, provider)
    assert status["status"] == "completed"
    assert status["partitions"]["unresolved"] == 4
    assert catalog.count() == 1
    assert len([r for r in provider.calls if r.url.path == "/api/skills"]) == 8


def test_request_budget_includes_discovery_and_resumes_next_page(catalog):
    def skills(request):
        number = int(request.url.params["page"])
        return page([skill_row(str(number))], number, more=number == 1)

    provider = Provider(skills)
    first = run(catalog, provider, max_requests=4)
    assert first["status"] == "paused" and first["requests"] == 4
    assert catalog.count() == 1
    second = run(catalog, provider)
    requests = [r for r in provider.calls if r.url.path == "/api/skills"]
    assert [r.url.params["page"] for r in requests[:2]] == ["1", "2"]
    assert second["status"] == "completed" and catalog.count() == 2


@pytest.mark.parametrize("retry_after, gaps", [
    (None, [2, 4, 8]),
    ("1", [2, 4, 8]),
    ("10", [10, 10, 10]),
    ("date", [10, 10, 10]),
    ("invalid", [2, 4, 8]),
    ("past-date", [2, 4, 8]),
])
def test_rate_limits_retry_same_page_and_finish(catalog, clock, retry_after, gaps):
    attempts = []

    def skills(request):
        number = int(request.url.params["page"])
        if number == 2:
            attempts.append((clock.time(), request.url))
            if len(attempts) <= 3:
                value = retry_after
                if value in ("date", "past-date"):
                    deadline = clock.time() + (10 if value == "date" else -10)
                    value = format_datetime(datetime.fromtimestamp(deadline, UTC), usegmt=True)
                return httpx.Response(429, headers={"Retry-After": value} if value else {})
        return page([skill_row(str(number))], number, more=number == 1)

    provider = Provider(skills)
    status = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()

    assert status["status"] == "completed"
    assert catalog.count() == 2 and status["added"] == 2
    assert len({url for _, url in attempts[:4]}) == 1
    assert [second[0] - first[0] for first, second in pairwise(attempts[:4])] == gaps
    assert status["requests"] == len(provider.calls)


def test_quota_pauses_without_skipping_page_and_resume_respects_retry_after(catalog, clock):
    provider = Provider(lambda _: httpx.Response(429, headers={"Retry-After": "10"}))
    status = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert status["status"] == "paused" and "429" in status["last_error"]
    assert status["partitions"]["pending"] == 4 and catalog.count() == 0
    requests = [r for r in provider.calls if r.url.path == "/api/skills"]
    assert len(requests) == 4 and len({r.url for r in requests}) == 1
    assert status["retry_after"] == clock.time() + 10

    provider.skills = lambda _: page([skill_row()])
    clock.sleeps.clear()
    next_call = len(provider.calls)
    resumed = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert clock.sleeps[0] == 10
    assert provider.calls[next_call].url == requests[-1].url
    assert resumed["status"] == "completed" and catalog.count() == 1


def test_request_budget_stops_rate_limit_retries_before_sleeping(catalog, clock):
    provider = Provider(lambda _: httpx.Response(429, headers={"Retry-After": "10"}))
    status = Sync(catalog, provider.client, max_requests=4,
                  sleep=clock.sleep, monotonic=clock.time).run()

    assert status["status"] == "paused" and status["requests"] == 4
    assert "budget" in status["last_error"].lower()
    assert clock.sleeps == [2, 2, 2]
    assert catalog.count() == 0 and status["partitions"]["failed"] == 0
    assert status["retry_after"] == clock.time() + 10

    failed_url = provider.calls[-1].url
    provider.skills = lambda _: page([skill_row()])
    resumed_at = []
    original = provider.client.get

    def get(*args, **kwargs):
        resumed_at.append(clock.time())
        return original(*args, **kwargs)

    provider.client.get = get
    resumed = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert provider.calls[4].url == failed_url
    assert resumed_at[0] == status["retry_after"]
    assert resumed["status"] == "completed" and catalog.count() == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_access_failures_pause_without_retrying(catalog, status_code):
    provider = Provider(lambda _: httpx.Response(status_code))
    status = run(catalog, provider)
    assert status["status"] == "paused" and str(status_code) in status["last_error"]
    assert len([r for r in provider.calls if r.url.path == "/api/skills"]) == 1
    assert catalog.count() == 0


@pytest.mark.parametrize("status_code", [408, 429, 503])
def test_transient_failures_retry_at_most_three_times(catalog, status_code):
    provider = Provider(lambda _: httpx.Response(status_code))
    status = run(catalog, provider)
    assert status["status"] == "paused"
    assert len([r for r in provider.calls if r.url.path == "/api/skills"]) == 4
    assert catalog.count() == 0
    provider.skills = lambda _: page([skill_row()])
    assert run(catalog, provider)["status"] == "completed"
    assert catalog.count() == 1


def test_malformed_rows_are_counted_but_malformed_pages_pause(catalog):
    provider = Provider(lambda _: page([skill_row(), {"id": "broken"}, None]))
    status = run(catalog, provider)
    assert catalog.count() == 1 and status["invalid"] == 8
    provider.skills = lambda _: {"skills": [], "pagination": {}}
    status = run(catalog, provider)
    assert status["status"] == "paused"
    assert status["partitions"]["failed"] == 1
    assert catalog.count() == 1


def test_estimated_uncapped_end_is_not_subdivided(catalog):
    provider = Provider(lambda _: page([skill_row()], exact=False))
    status = run(catalog, provider)
    assert status["partitions"]["completed"] == 4
    assert status["partitions"]["capped"] == 0
    assert status["warnings"]


def test_bundled_data_wins_and_alias_is_recorded(catalog):
    bundled = import_skill(skill_row()).model_copy(update={
        "provenance": "bundled", "description": "Reviewed bundled description", "license": "MIT"})
    catalog.upsert(bundled)
    run(catalog, Provider(lambda _: page([skill_row()])))
    assert catalog.get(bundled.id) == bundled
    with catalog.db_sessions() as session:
        alias = session.get(CatalogAliasRecord, ("skillsmp", "one"))
        assert alias.skill_id == bundled.id
        assert json.loads(alias.payload)["description"] == skill_row()["description"]


@pytest.mark.parametrize("path", ["SKILL.md", "skills/one/SKILL.md", "skills/one"])
def test_directory_and_provider_paths_do_not_duplicate_segments(path):
    imported = import_skill(skill_row(path=path))
    assert imported.id == stable_id("example/repo", "skills/one/SKILL.md")
    assert imported.source_url.endswith("/main/skills/one/SKILL.md")


@pytest.mark.parametrize("url", ["http://github.com/example/repo", "https://github.com.evil.test/x/y",
                                   "https://user@github.com/x/y", "https://github.com/x/y/tree/main/../bad"])
def test_untrusted_source_urls_are_not_imported(url):
    with pytest.raises(ValueError):
        import_skill(skill_row(githubUrl=url))


def test_repository_root_skill_and_missing_optional_metadata():
    imported = import_skill(skill_row(githubUrl="https://github.com/example/repo/tree/main",
                                     description=None, author=None))
    assert imported.id == stable_id("example/repo", "SKILL.md")
    assert imported.source_url == "https://github.com/example/repo/blob/main/SKILL.md"
    assert imported.description == "" and imported.author == ""


def test_requests_are_serial_and_at_least_two_seconds_apart(catalog):
    elapsed = [0.0]
    starts = []
    provider = Provider(lambda _: page([skill_row()]))
    original = provider.client.get

    def get(*args, **kwargs):
        starts.append(elapsed[0])
        return original(*args, **kwargs)

    provider.client.get = get

    def sleep(seconds):
        elapsed[0] += seconds

    Sync(catalog, provider.client, sleep=sleep, monotonic=lambda: elapsed[0]).run()
    assert len(starts) >= 7
    assert all(second - first >= 2 for first, second in pairwise(starts))


@pytest.mark.parametrize("route", ["javascript://skillsmp.com/foo", "http://skillsmp.com/foo",
                                  "https://other.example/foo"])
def test_provider_links_require_https_and_provider_host(route):
    assert import_skill(skill_row(route=route)).provider_url is None


def test_capped_expansion_only_enqueues_newly_discovered_values(catalog):
    provider = Provider(lambda _: page([skill_row()], capped=True))
    sync = Sync(catalog, provider.client, sleep=lambda _: None)
    sync.state["discovered"]["language"] = ["en"]
    key = json.dumps({"filters": {}, "sort": "stars"}, sort_keys=True)
    with catalog.db_sessions() as session:
        data = json.loads(session.get(SyncPartitionRecord, key).data)
    sync.page(key, data)
    # An unchanged expansion must do no queue writes, even on subsequent capped pages.
    original = sync.enqueue
    def reject_enqueue(*args, **kwargs):
        pytest.fail("Repeated expansion attempted a queue write")
    sync.enqueue = reject_enqueue
    assert sync.expand(data) is False
    sync.enqueue = original
    sync.add_filter("language", "ko")
    with catalog.db_sessions() as session:
        queued = [json.loads(data) for data in session.scalars(select(SyncPartitionRecord.data))]
    assert any(row["filters"] == {"language": "ko"} for row in queued)
    root = next(row for row in queued if row["filters"] == {} and row["sort"] == "stars")
    assert root["expanded_values"] == 2


def test_interrupted_upsert_keeps_authoritative_unique_inventory(catalog, monkeypatch):
    provider = Provider(lambda _: page([skill_row()]))
    original = catalog.upsert
    def interrupt_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(catalog, "upsert", interrupt_after_write)
    with pytest.raises(KeyboardInterrupt):
        run(catalog, provider)
    status = sync_status(catalog)
    assert status["status"] == "paused"
    assert status["imported_unique_total"] == 1 and status["added"] == 0
    monkeypatch.setattr(catalog, "upsert", original)
    assert run(catalog, provider)["imported_unique_total"] == 1


@pytest.mark.parametrize("interrupted", [False, True])
def test_cli_exit_codes_and_indexing_after_pause(catalog, monkeypatch, tmp_path, capsys, interrupted):
    import sys
    from types import SimpleNamespace

    from skinder import sync as module

    def skills(request):
        assert 1 <= int(request.url.params["limit"]) <= 48
        if interrupted:
            raise KeyboardInterrupt
        return page([skill_row()])

    provider = Provider(skills)
    original = module.Sync
    monkeypatch.setattr(module, "Sync", lambda *args, **kwargs:
                        original(*args, **kwargs, sleep=lambda _: None))
    def client(**kwargs):
        assert "Skinder" in kwargs["headers"]["User-Agent"]
        return provider.client
    monkeypatch.setattr(module.httpx, "Client", client)
    marker = tmp_path / "indexed"
    def index(catalog, *, allow_download):
        marker.write_text(str(catalog.count()))
        return {"indexed_count": catalog.count()}
    monkeypatch.setitem(sys.modules, "skinder.retrieval", SimpleNamespace(index_catalog=index))
    with pytest.raises(SystemExit) as exit_info:
        module.main(["--catalog-db-path", str(catalog.path), "--max-requests", "4"])
    assert exit_info.value.code == (130 if interrupted else 2)
    report = json.loads(capsys.readouterr().out)
    assert report["sync"]["status"] == "paused"
    if interrupted:
        assert not marker.exists()
    else:
        assert marker.read_text() == "1"
        assert report["index"]["indexed_count"] == 1
