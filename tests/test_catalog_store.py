from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select

from skinder.catalog import CatalogStore, stable_id
from skinder.models import Skill
from skinder.orm import CatalogAliasRecord


def example_skill(number, **changes):
    values = {
        "id": f"example-{number}", "name": f"Skill {number:03}", "slug": f"skill-{number}",
        "description": "A useful workflow.", "category": "Development", "author": "example",
        "repository": "example/skills",
        "source_url": f"https://github.com/example/skills/tree/main/skills/{number}",
        "revision": None, "license": None, "compatibility": None, "requirements": [],
        "tags": [], "workflow": [], "output_style": [], "evidence": [],
        "install_commands": {}, "provenance": "skillsmp",
    }
    values.update(changes)
    return Skill.model_validate(values)


def test_pages_search_and_eligibility_use_persisted_catalog(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    for number in range(65):
        store.upsert(example_skill(number, description="PostgreSQL backups"))
    store.upsert(example_skill(70, description="Draw illustrations", category="Art"))
    assert store.page().total == 66
    assert store.page().has_more
    assert len(store.page().skills) == 50
    tail = store.page(offset=50)
    assert len(tail.skills) == 16
    assert not tail.has_more
    assert {s.id for s in store.page().skills}.isdisjoint(s.id for s in tail.skills)
    assert [s.id for s in store.page(q="illustrations", category="Art").skills] == ["example-70"]
    assert store.page(q='" OR * (').skills == []
    store.upsert(example_skill(70, author="unique-maintainer", category="Art"))
    assert [skill.id for skill in store.page(q="unique-maintainer").skills] == ["example-70"]
    store.upsert(example_skill(0, description="PostgreSQL backups", compatibility=["claude-code"]))
    matches = store.keyword_search("PostgreSQL", agent="codex", excluded=["example-1"], limit=3)
    assert len(matches) == 3
    assert not {"example-0", "example-1"} & {s.id for s in matches}
    assert [s.id for s in CatalogStore(store.path).eligible(
        ["example-0", "example-1", "example-2"], agent="codex", excluded=["example-1"]
    )] == ["example-2"]


def test_repeat_import_updates_search_and_preserves_bundled_evidence(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    original = example_skill(1, description="Apple orchards")
    assert store.upsert(original, provider_id="upstream", raw={"stars": 1}) == "added"
    assert store.upsert(original, provider_id="upstream", raw={"stars": 2}) == "unchanged"
    changed = original.model_copy(update={"description": "Orange orchards"})
    store.upsert(changed, provider_id="upstream")
    assert store.count() == 1
    assert store.keyword_search("Apple") == []
    assert [s.id for s in store.keyword_search("Orange")] == [original.id]
    bundled = changed.model_copy(update={"provenance": "bundled", "workflow": ["Reviewed method"]})
    store.upsert(bundled)
    store.upsert(original, provider_id="upstream", raw={"description": "Apple orchards"})
    assert store.get(original.id).workflow == ["Reviewed method"]
    assert store.get(original.id).description == "Orange orchards"
    with store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogAliasRecord)) == 1


def test_ids_match_bundled_directory_identity_and_survive_revision():
    assert stable_id("Example/Skills", "skills/review/SKILL.md") == stable_id(
        "example/skills", "skills/review/SKILL.md"
    )
    assert stable_id("example/skills", "skills/Review/SKILL.md") != stable_id(
        "example/skills", "skills/review/SKILL.md"
    )


def test_keyword_exclusions_are_bounded_and_do_not_leak_between_sessions(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.upsert(example_skill(1, description="Database backups"))
    excluded = [f"missing-{number}" for number in range(40_000)] + ["example-1"]
    assert store.keyword_search("backups", excluded=excluded) == []
    assert [skill.id for skill in store.keyword_search("backups")] == ["example-1"]
    store.close()
    reopened = CatalogStore(store.path)
    assert reopened.get("example-1").description == "Database backups"
    assert [skill.id for skill in reopened.keyword_search("backups")] == ["example-1"]


def test_concurrent_imports_keep_one_skill_and_all_aliases(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    skill = example_skill(1, description="Database backups")
    ready = Barrier(8)

    def import_alias(number):
        ready.wait()
        return store.upsert(skill, provider_id=f"provider-{number}")

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(import_alias, range(8)))

    assert outcomes.count("added") == 1
    assert outcomes.count("unchanged") == 7
    assert store.count() == 1
    assert [result.id for result in store.keyword_search("backups")] == [skill.id]
    with store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogAliasRecord)) == 8
