"""Offline provider-contract tests assert durable catalog and crawl outcomes."""

import json
from itertools import pairwise

import httpx
import pytest

from skinder.catalog import CatalogStore, stable_id
from skinder.sync import Sync, import_skill, sync_status


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


def run(catalog, provider, **kwargs):
    return Sync(catalog, provider.client, sleep=lambda _: None, **kwargs).run()


@pytest.fixture
def catalog(tmp_path):
    return CatalogStore(tmp_path / "catalog.sqlite3")


def test_status_read_does_not_create_sync_state(catalog):
    assert sync_status(catalog) is None
    with catalog.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='sync_run'").fetchone()


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


def test_quota_pauses_without_skipping_page_and_resume_respects_retry_after(catalog):
    provider = Provider(lambda _: httpx.Response(429, headers={"Retry-After": "10"}))
    status = run(catalog, provider)
    assert status["status"] == "paused" and "429" in status["last_error"]
    assert status["partitions"]["pending"] == 4 and catalog.count() == 0
    provider.skills = lambda _: page([skill_row()])
    sleeps = []
    resumed = Sync(catalog, provider.client, sleep=sleeps.append).run()
    assert sleeps[0] > 9
    assert resumed["status"] == "completed" and catalog.count() == 1


def test_transient_failures_retry_at_most_three_times(catalog):
    provider = Provider(lambda _: httpx.Response(503))
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
    with catalog.connect() as db:
        alias = db.execute("SELECT skill_id,payload FROM catalog_aliases WHERE external_id='one'").fetchone()
    assert alias["skill_id"] == bundled.id
    assert json.loads(alias["payload"])["description"] == skill_row()["description"]


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
