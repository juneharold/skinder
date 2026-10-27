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


def test_pages_search_and_eligibility_use_persisted_catalog(db):
    catalog_store = CatalogStore(db)
    for number in range(65):
        catalog_store.upsert(example_skill(number, description="PostgreSQL backups"))
    catalog_store.upsert(example_skill(70, description="Draw illustrations", category="Art"))
    assert catalog_store.page().total == 66
    assert catalog_store.page().has_more
    assert len(catalog_store.page().skills) == 50
    tail = catalog_store.page(offset=50)
    assert len(tail.skills) == 16
    assert not tail.has_more
    assert {s.id for s in catalog_store.page().skills}.isdisjoint(s.id for s in tail.skills)
    assert [s.id for s in catalog_store.page(q="illustrations", category="Art").skills] == ["example-70"]
    assert catalog_store.page(q='" OR * (').skills == []
    catalog_store.upsert(example_skill(70, author="unique-maintainer", category="Art"))
    assert [skill.id for skill in catalog_store.page(q="unique-maintainer").skills] == ["example-70"]
    catalog_store.upsert(example_skill(0, description="PostgreSQL backups", compatibility=["claude-code"]))
    matches = catalog_store.keyword_search("PostgreSQL", agent="codex", excluded=["example-1"], limit=3)
    assert len(matches) == 3
    assert not {"example-0", "example-1"} & {s.id for s in matches}
    assert [s.id for s in CatalogStore(db).eligible(
        ["example-0", "example-1", "example-2"], agent="codex", excluded=["example-1"]
    )] == ["example-2"]


def test_repeat_import_updates_search_and_preserves_bundled_evidence(db):
    catalog_store = CatalogStore(db)
    original = example_skill(1, description="Apple orchards")
    assert catalog_store.upsert(original, provider_id="upstream", raw={"stars": 1}) == "added"
    assert catalog_store.upsert(original, provider_id="upstream", raw={"stars": 2}) == "unchanged"
    changed = original.model_copy(update={"description": "Orange orchards"})
    catalog_store.upsert(changed, provider_id="upstream")
    assert catalog_store.count() == 1
    assert catalog_store.keyword_search("Apple") == []
    assert [s.id for s in catalog_store.keyword_search("Orange")] == [original.id]
    bundled = changed.model_copy(update={"provenance": "bundled", "workflow": ["Reviewed method"]})
    catalog_store.upsert(bundled)
    catalog_store.upsert(original, provider_id="upstream", raw={"description": "Apple orchards"})
    assert catalog_store.get(original.id).workflow == ["Reviewed method"]
    assert catalog_store.get(original.id).description == "Orange orchards"
    with catalog_store.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogAliasRecord)) == 1


def test_blank_queries_list_skills_but_punctuation_only_queries_do_not(db):
    catalog_store = CatalogStore(db)
    catalog_store.upsert(example_skill(1, category="Art"))
    catalog_store.upsert(example_skill(2, category="Development"))

    for query in ("", " \t\n"):
        result = catalog_store.page(q=query, category="Art")
        assert [skill.id for skill in result.skills] == ["example-1"]
        assert result.total == 1
        assert not result.has_more

    result = catalog_store.page(q="***", category="Art")
    assert result.skills == []
    assert result.total == 0
    assert not result.has_more
    assert result.categories == ["Art", "Development"]


def test_ids_match_bundled_directory_identity_and_survive_revision():
    assert stable_id("Example/Skills", "skills/review/SKILL.md") == stable_id(
        "example/skills", "skills/review/SKILL.md"
    )
    assert stable_id("example/skills", "skills/Review/SKILL.md") != stable_id(
        "example/skills", "skills/review/SKILL.md"
    )


def test_keyword_exclusions_are_bounded_and_do_not_leak_between_sessions(db):
    catalog_store = CatalogStore(db)
    catalog_store.upsert(example_skill(1, description="Database backups"))
    excluded = [f"missing-{number}" for number in range(40_000)] + ["example-1"]
    assert catalog_store.keyword_search("backups", excluded=excluded) == []
    assert [skill.id for skill in catalog_store.keyword_search("backups")] == ["example-1"]
    catalog_store.close()
    reopened = CatalogStore(db)
    assert reopened.get("example-1").description == "Database backups"
    assert [skill.id for skill in reopened.keyword_search("backups")] == ["example-1"]


def test_concurrent_imports_keep_one_skill_and_all_aliases(db):
    catalog_store = CatalogStore(db)
    skill = example_skill(1, description="Database backups")
    ready = Barrier(8)

    def import_alias(number):
        ready.wait()
        return catalog_store.upsert(skill, provider_id=f"provider-{number}")

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(import_alias, range(8)))

    assert outcomes.count("added") == 1
    assert outcomes.count("unchanged") == 7
    assert catalog_store.count() == 1
    assert [result.id for result in catalog_store.keyword_search("backups")] == [skill.id]
    with catalog_store.db_sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogAliasRecord)) == 8


def test_unicode_words_are_literal_and_search_weights_favor_name(catalog):
    catalog.upsert(example_skill(1, name="데이터", description="Ordinary workflow"))
    catalog.upsert(example_skill(2, name="Other", description="데이터 analysis"))
    assert [skill.id for skill in catalog.keyword_search("데이터 | nonexistent")] == [
        "example-1", "example-2"]
    assert catalog.keyword_search("!!!") == []


def test_concurrent_provider_import_cannot_replace_bundled_evidence(catalog):
    ready = Barrier(2)
    bundled = example_skill(1, provenance="bundled", workflow=["Reviewed instructions"])
    imported = example_skill(1, description="Provider metadata")

    def write(skill):
        ready.wait()
        catalog.upsert(skill, provider_id="listing" if skill.provenance == "skillsmp" else None)

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(write, [bundled, imported]))
    assert catalog.get(bundled.id) == bundled
    with catalog.db_sessions() as session:
        assert session.get(CatalogAliasRecord, ("skillsmp", "listing")).skill_id == bundled.id
