"""Offline tests cover the public search contract and durable importer outcomes."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import pairwise

import httpx
import pytest
from sqlalchemy import func, select

from skinder.catalog import CatalogStore, stable_id
from skinder.orm import CatalogAliasRecord, SyncPartitionRecord, SyncRunRecord
from skinder.sync import Sync, import_skill, sync_status


def skill_row(name="one", **changes):
    return {"id": name, "name": name, "description": "A provider description.",
            "githubUrl": f"https://github.com/example/repo/tree/main/skills/{name}",
            "author": "example", "contentLanguage": "en",
            "skillUrl": f"https://skillsmp.com/skills/{name}", **changes}


def page(rows, number=1, *, more=False, capped=False):
    return {"success": True, "data": {"skills": rows, "pagination": {
        "page": number, "limit": 50, "hasNext": more, "hasPrev": number > 1,
        "total": 50, "totalPages": 1, "totalIsExact": False,
        "isCapped": capped, "maxResults": 5000,
    }}}


class Provider:
    def __init__(self, skills):
        self.skills, self.calls = skills, []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append(request)
        assert request.url.path == "/api/v1/skills/search"
        assert request.url.params["q"] in {
            "coding", "testing", "design", "data", "documentation", "automation",
        }
        assert request.url.params["limit"] == "50"
        assert request.url.params["sortBy"] == "stars"
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
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("skinder.sync.time.time", clock.time)
    return clock


def test_status_read_does_not_create_sync_state(catalog):
    assert sync_status(catalog) is None
    with catalog.db_sessions() as session:
        assert session.get(SyncRunRecord, 1) is None
        assert session.scalar(select(func.count()).select_from(SyncPartitionRecord)) == 0


def test_overlap_preserves_source_identity_metadata_and_aliases(catalog):
    provider = Provider(lambda _: page([skill_row()]))
    status = run(catalog, provider)
    skill = catalog.get(stable_id("example/repo", "skills/one/SKILL.md"))
    assert catalog.count() == status["imported_unique_total"] == 1
    assert status["added"] == 1 and status["duplicates"] == 5
    assert skill.source_url == "https://github.com/example/repo/blob/main/skills/one/SKILL.md"
    assert skill.provider_url == "https://skillsmp.com/skills/one"
    assert skill.provenance == "skillsmp" and skill.content_language == "en"
    assert skill.category == "Uncategorized" and skill.tags == []
    assert skill.revision is None and skill.license is None and skill.compatibility is None
    assert run(catalog, provider)["added"] == 0
    assert catalog.get(skill.id) == skill


def test_budget_resumes_pages_and_gives_every_keyword_a_turn(catalog):
    def skills(request):
        number = int(request.url.params["page"])
        return page([skill_row(str(number))], number, more=number < 3)
    provider = Provider(skills)
    first = run(catalog, provider, max_requests=7)
    assert first["status"] == "paused" and first["pause_reason"] == "budget"
    assert first["requests"] == 7 and catalog.count() == 2
    assert {r.url.params["q"] for r in provider.calls[:6]} == set(catalog.settings.sync_keywords)
    assert all(r.url.params["page"] == "1" for r in provider.calls[:6])
    second = run(catalog, provider)
    assert second["status"] == "completed" and catalog.count() == 3
    assert provider.calls[7].url.params["q"] == "testing"
    assert provider.calls[7].url.params["page"] == "2"
    assert len({r.url for r in provider.calls}) == 18


def test_obsolete_crawl_state_migrates_without_removing_inventory(catalog):
    catalog.upsert(import_skill(skill_row()))
    with catalog.db_sessions.begin() as session:
        session.add(SyncRunRecord(id=1, data={"status": "paused", "discovery_pending": ["/categories"],
                    "discovery_seen": [], "discovered": {}, "requests": 1}))
        session.add(SyncPartitionRecord(key="{}", status="pending", data={"filters": {}}))
    status = run(catalog, Provider(lambda _: page([skill_row()])))
    assert status["status"] == "completed" and catalog.count() == 1
    assert status["partitions"]["completed"] == 6
    with catalog.db_sessions() as session:
        assert session.get(SyncPartitionRecord, "{}") is None
        assert "discovery_pending" not in session.get(SyncRunRecord, 1).data


@pytest.mark.parametrize("kind", ["repeated", "canonical", "empty"])
def test_nonadvancing_results_stop_current_scan_and_refresh_in_later_runs(catalog, kind):
    def skills(request):
        number = int(request.url.params["page"])
        rows = [] if kind == "empty" else [skill_row()]
        return page(rows, 1 if kind == "canonical" else number, more=True)
    provider = Provider(skills)
    status = run(catalog, provider)
    assert status["partitions"]["unresolved"] == 6
    assert catalog.count() == (0 if kind == "empty" else 1)
    calls = len(provider.calls)
    run(catalog, provider)
    assert len(provider.calls) == calls * 2


def test_capped_windows_finish_unresolved_without_inventing_queries(catalog):
    provider = Provider(lambda _: page([skill_row()], capped=True))
    status = run(catalog, provider)
    assert status["partitions"]["unresolved"] == status["partitions"]["capped"] == 6
    assert len(provider.calls) == 6
    run(catalog, provider)
    assert len(provider.calls) == 12


def test_uncertain_completion_is_never_automatically_replayed(catalog):
    provider = Provider(lambda _: httpx.Response(503, json={"success": False,
                        "error": {"code": "SEARCH_COMPLETION_UNCERTAIN"}}))
    first = run(catalog, provider)
    assert first["status"] == "paused" and first["partitions"]["unresolved"] == 1
    assert len(provider.calls) == 1
    provider.skills = lambda _: page([skill_row()])
    run(catalog, provider)
    run(catalog, provider)
    assert sum(r.url.params["q"] == "coding" for r in provider.calls) == 1
    assert catalog.count() == 1


@pytest.mark.parametrize("retry_after", ["10", "date", "invalid", None])
def test_transient_retries_honor_pacing_and_retry_after(catalog, clock, retry_after):
    starts = []
    def skills(request):
        starts.append(clock.time())
        if len(starts) <= 3:
            value = retry_after
            if value == "date":
                value = format_datetime(datetime.fromtimestamp(clock.time() + 10, UTC), usegmt=True)
            return httpx.Response(429, headers={"Retry-After": value} if value else {})
        return page([skill_row()])
    provider = Provider(skills)
    status = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert status["status"] == "completed" and catalog.count() == 1
    expected = [10, 10, 10] if retry_after in ("10", "date") else [6, 6, 8]
    assert [b - a for a, b in pairwise(starts[:4])] == expected
    assert len({r.url for r in provider.calls[:4]}) == 1
    assert status["requests"] == len(provider.calls) == 9


def test_retry_budget_stops_before_sleep_and_persists_cooldown(catalog, clock):
    provider = Provider(lambda _: httpx.Response(429, headers={"Retry-After": "10"}))
    first = Sync(catalog, provider.client, max_requests=2,
                 sleep=clock.sleep, monotonic=clock.time).run()
    assert first["pause_reason"] == "budget" and len(provider.calls) == 2
    assert clock.sleeps == [10]
    provider.skills = lambda _: page([skill_row()])
    first_url = provider.calls[-1].url
    clock.sleeps.clear()
    resumed = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert resumed["status"] == "completed" and catalog.count() == 1
    assert provider.calls[2].url == first_url and clock.sleeps[0] == 10


def test_long_cooldown_does_not_block_process_or_restart(catalog, clock):
    provider = Provider(lambda _: httpx.Response(503, headers={"Retry-After": "3600"}))
    first = Sync(catalog, provider.client, sleep=clock.sleep, monotonic=clock.time).run()
    assert first["status"] == "paused" and len(provider.calls) == 1 and not clock.sleeps
    run(catalog, provider)
    assert len(provider.calls) == 1
    clock.elapsed += 3600
    provider.skills = lambda _: page([])
    assert run(catalog, provider)["status"] == "completed"


@pytest.mark.parametrize("failure", ["header", "code"])
def test_provider_daily_exhaustion_is_persisted_until_next_utc_day(catalog, clock, failure):
    if failure == "header":
        response = httpx.Response(200, json=page([skill_row()]),
                                  headers={"X-RateLimit-Daily-Remaining": "0"})
    else:
        response = httpx.Response(429, json={"success": False,
                                  "error": {"code": "DAILY_QUOTA_EXCEEDED"}})
    provider = Provider(lambda _: response)
    run(catalog, provider)
    run(catalog, provider)
    assert len(provider.calls) == 1
    clock.elapsed = (int(clock.time()) // 86400 + 1) * 86400
    provider.skills = lambda _: page([skill_row()])
    assert run(catalog, provider)["status"] == "completed"
    assert catalog.count() == 1


def test_daily_budget_survives_completed_runs_and_resets_at_midnight(catalog, clock):
    settings = catalog.settings.model_copy(update={"sync_daily_requests": 7})
    provider = Provider(lambda _: page([skill_row()]))
    run(catalog, provider, settings=settings)
    status = run(catalog, provider, settings=settings)
    assert len(provider.calls) == 7 and status["pause_reason"] == "budget"
    run(catalog, provider, settings=settings)
    assert len(provider.calls) == 7
    clock.elapsed = (int(clock.time()) // 86400 + 1) * 86400
    assert run(catalog, provider, settings=settings)["status"] == "completed"
    assert len(provider.calls) == 12


@pytest.mark.parametrize("code", [401, 403])
def test_access_failures_are_not_retried_after_process_restart(catalog, clock, code):
    provider = Provider(lambda _: httpx.Response(code))
    assert run(catalog, provider)["status"] == "paused"
    run(catalog, provider)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("code", [408, 429, 503])
def test_transient_failure_retries_are_bounded(catalog, code):
    provider = Provider(lambda _: httpx.Response(code))
    status = run(catalog, provider)
    assert status["status"] == "paused" and status["pause_reason"] == "provider"
    assert len(provider.calls) == 4 and catalog.count() == 0


def test_malformed_rows_are_skipped_but_bad_pagination_keeps_checkpoint(catalog):
    provider = Provider(lambda _: page([skill_row(), None, {"id": "bad"}]))
    status = run(catalog, provider)
    assert catalog.count() == 1 and status["invalid"] == 12
    provider.skills = lambda _: {"success": True, "data": {"skills": [], "pagination": {}}}
    status = run(catalog, provider)
    assert status["status"] == "paused" and status["partitions"]["failed"] == 1
    with catalog.db_sessions() as session:
        assert session.get(SyncPartitionRecord, "coding").data["page"] == 1


def test_pacing_is_preserved_between_separate_invocations(catalog, clock):
    starts = []
    def skills(request):
        starts.append(clock.time())
        return page([skill_row()])
    provider = Provider(skills)
    for _ in range(2):
        Sync(catalog, provider.client, max_requests=3,
             sleep=clock.sleep, monotonic=clock.time).run()
    assert len(starts) == 6
    assert all(b - a >= 6 for a, b in pairwise(starts))


@pytest.mark.parametrize("outcome, expected_exit", [("budget", None), ("provider", 2), ("interrupt", 130)])
def test_cli_indexes_completed_batches_and_reports_exit_status(catalog, monkeypatch, capsys, outcome, expected_exit):
    import sys
    from types import SimpleNamespace

    from skinder import sync as module

    def skills(request):
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "provider":
            return httpx.Response(403)
        return page([skill_row()])
    provider = Provider(skills)
    original = module.Sync
    monkeypatch.setattr(module, "load_settings", lambda role: catalog.settings)
    monkeypatch.setattr(module, "Database", lambda settings: catalog.database)
    monkeypatch.setattr(module, "Sync", lambda *args, **kwargs:
                        original(*args, **kwargs, sleep=lambda _: None))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: provider.client)
    indexed = []
    def index(catalog, *, allow_download):
        indexed.append(catalog.count())
        return {"indexed_count": catalog.count()}
    monkeypatch.setitem(sys.modules, "skinder.retrieval", SimpleNamespace(index_catalog=index))
    if expected_exit is None:
        module.main(["--max-requests", "1"])
    else:
        with pytest.raises(SystemExit) as caught:
            module.main(["--max-requests", "1"])
        assert caught.value.code == expected_exit
    report = json.loads(capsys.readouterr().out)
    assert report["sync"]["status"] == "paused"
    assert indexed == ([] if outcome == "interrupt" else [1 if outcome == "budget" else 0])


def test_bundled_data_wins_and_alias_is_recorded(catalog):
    bundled = import_skill(skill_row()).model_copy(update={
        "provenance": "bundled", "description": "Reviewed bundled description", "license": "MIT"})
    catalog.upsert(bundled)
    run(catalog, Provider(lambda _: page([skill_row()])))
    assert catalog.get(bundled.id) == bundled
    with catalog.db_sessions() as session:
        alias = session.get(CatalogAliasRecord, ("skillsmp", "one"))
        assert alias.skill_id == bundled.id
        assert alias.payload["description"] == skill_row()["description"]


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


@pytest.mark.parametrize("route", ["javascript://skillsmp.com/foo", "http://skillsmp.com/foo",
                                  "https://other.example/foo"])
def test_provider_links_require_https_and_provider_host(route):
    assert import_skill(skill_row(skillUrl=route)).provider_url is None


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


def test_advisory_lock_excludes_other_writers_and_releases_without_transaction(catalog):
    from sqlalchemy import text

    from skinder.sync import writer_lock

    with writer_lock(catalog):
        with pytest.raises(RuntimeError, match="Another catalog"), writer_lock(catalog):
            pytest.fail("Concurrent writer entered")
        with catalog.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM pg_stat_activity a JOIN pg_locks l ON a.pid=l.pid "
                "WHERE l.locktype='advisory' AND l.objid=73519002 "
                "AND a.state = 'idle in transaction'")) == 0
    with writer_lock(catalog):
        catalog.upsert(import_skill(skill_row()))
    assert catalog.count() == 1


def test_writer_lock_does_not_use_the_only_catalog_pool_slot(db, settings):
    from skinder.database import Database
    from skinder.sync import writer_lock

    settings = settings.model_copy(update={"db_pool_size": 1, "db_pool_timeout_seconds": 0.2})
    database = Database(settings, schema=db.schema)
    catalog = CatalogStore(database, settings)
    try:
        with writer_lock(catalog):
            catalog.upsert(import_skill(skill_row()))
            assert catalog.count() == 1
    finally:
        database.close()


def test_live_api_shape_preserves_source_and_provider_updated_time(catalog):
    from skinder.orm import CatalogSkillRecord

    row = {
        "id": "openclaw-openclaw-skills-coding-agent-skill-md", "name": "coding-agent",
        "author": "openclaw", "description": "Delegate coding tasks.", "contentLanguage": "en",
        "githubUrl": "https://github.com/openclaw/openclaw/tree/main/skills/coding-agent",
        "skillUrl": "https://skillsmp.com/creators/openclaw/openclaw/skills-coding-agent",
        "stars": 100, "updatedAt": 1785303822,
    }
    run(catalog, Provider(lambda _: page([row])))
    identifier = stable_id("openclaw/openclaw", "skills/coding-agent/SKILL.md")
    skill = catalog.get(identifier)
    assert skill.source_url == "https://github.com/openclaw/openclaw/blob/main/skills/coding-agent/SKILL.md"
    assert skill.provider_url == row["skillUrl"]
    with catalog.db_sessions() as session:
        assert session.get(CatalogSkillRecord, identifier).source_updated_at == "1785303822"
        alias = session.get(CatalogAliasRecord, ("skillsmp", row["id"]))
        assert alias.skill_id == identifier and alias.payload == row


def test_transport_retries_consume_invocation_and_persisted_daily_budgets(catalog):
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    provider = Provider(offline)
    first = run(catalog, provider, max_requests=2)
    assert len(provider.calls) == first["daily_requests"] == 2
    assert first["pause_reason"] == "budget" and catalog.count() == 0
    provider.skills = lambda _: page([skill_row()])
    second = run(catalog, provider)
    assert second["daily_requests"] == 8 and catalog.count() == 1
    assert provider.calls[0].url == provider.calls[2].url
