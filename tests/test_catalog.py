"""Catalog contract and refresh behavior; all tests run without network access."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import shlex

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("refresh_catalog", ROOT / "scripts/refresh_catalog.py")
refresh_catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh_catalog)
DATA = ROOT / "src/skinder/data"


def snapshot():
    return json.loads((DATA / "catalog.json").read_text())


def source_manifest():
    return json.loads((DATA / "sources.json").read_text())


def test_release_is_diverse_real_and_fully_pinned():
    catalog = snapshot()["skills"]
    manifest = source_manifest()
    assert 100 <= len(catalog) <= 200
    assert len({s["id"] for s in catalog}) == len(catalog)
    assert len({s["author"] for s in catalog}) >= 5
    assert {s["category"] for s in catalog} == refresh_catalog.CATEGORIES
    assert all(sum(s["category"] == category for s in catalog) >= 5 for category in refresh_catalog.CATEGORIES)
    selected = {(source["repository"], entry["path"]): (source, entry)
                for source in manifest["sources"] for entry in source["skills"]}
    assert len(selected) == len(catalog)
    for skill in catalog:
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", skill["id"])
        source, entry = next((source, entry) for (repo, path), (source, entry) in selected.items()
                             if skill["source_url"] == f"https://github.com/{repo}/blob/{source['revision']}/{path}")
        assert skill["revision"] == source["revision"]
        assert skill["id"] == refresh_catalog.stable_id(source["repository"], entry["path"])
        assert re.fullmatch(r"[0-9a-f]{64}", entry["source_sha256"])
        refresh_catalog.validate_skill(skill)


def test_offline_snapshot_is_derived_from_manifest():
    assert refresh_catalog.compile_snapshot(source_manifest()) == snapshot()


def test_install_commands_select_only_supported_agents_and_pinned_directories():
    for skill in snapshot()["skills"]:
        assert set(skill["install_commands"]) <= set(skill["compatibility"])
        for agent, command in skill["install_commands"].items():
            args = shlex.split(command)
            assert args[:3] == ["npx", "skills", "add"]
            assert args[3].startswith(f"https://github.com/{skill['repository']}/tree/{skill['revision']}/")
            assert args[args.index("--agent") + 1] == agent
            assert "--yes" not in args and "--all" not in args and "-y" not in args
        if skill["repository"] == "obra/superpowers":
            assert not skill["install_commands"]
            assert any("companion" in requirement.lower() for requirement in skill["requirements"])


def test_licenses_and_support_inventory_remain_traceable():
    manifest = source_manifest()
    for source in manifest["sources"]:
        license_paths = {item["path"] for item in source["licenses"]}
        for item in source["licenses"]:
            text = manifest["license_texts"][item["sha256"]]
            assert refresh_catalog.digest(text) == item["sha256"]
            assert f"/blob/{source['revision']}/" in item["url"]
        for entry in source["skills"]:
            if entry["license_path"]:
                assert entry["license_path"] in license_paths
            else:
                assert entry["license"] == "See source license" or entry["declared_license"]
            if entry["inventory"]["helper_files"]:
                assert any(r.startswith("Bundled support:") for r in entry["requirements"])
    excluded = {"skills/docx/SKILL.md", "skills/pdf/SKILL.md", "skills/pptx/SKILL.md", "skills/xlsx/SKILL.md"}
    anthropic = next(s for s in manifest["sources"] if s["repository"] == "anthropics/skills")
    assert not excluded.intersection(e["path"] for e in anthropic["skills"])


@pytest.mark.parametrize("description,expected", [
    ('"A quoted purpose: with a colon."', "A quoted purpose: with a colon."),
    ("'A writer''s workflow.'", "A writer's workflow."),
    (">-\n  First line\n  second line", "First line second line"),
    ("|\n  First line\n  second line", "First line\nsecond line"),
])
def test_frontmatter_reads_real_string_forms_without_evaluating_yaml(description, expected):
    metadata, body = refresh_catalog.parse_skill(f"---\nname: example\ndescription: {description}\nmetadata:\n  author: someone\n---\nInspect first.\n")
    assert metadata["description"] == expected
    assert body == "Inspect first.\n"
    assert "author" not in metadata


def sample_manifest():
    manifest = source_manifest()
    source = next(s for s in manifest["sources"] if s["repository"] == "github/awesome-copilot")
    source["skills"] = [deepcopy(source["skills"][0])]
    entry = source["skills"][0]
    entry.update(path="skills/example/SKILL.md", name="Example", slug="example", declared_license="MIT", source_compatibility="",
                 workflow=["Inspect before changing."], output_style=[],
                 evidence=[{"trait": "Inspect before changing.", "excerpt": "Inspect before changing."}])
    source["licenses"] = source["licenses"][:1]
    source.pop("inventory_revision", None)
    manifest["sources"] = [source]
    return manifest


def source_text():
    return "---\nname: example\ndescription: Investigate first.\nlicense: MIT\n---\nInspect before changing.\nDo not run downloaded programs.\n"


def test_refresh_verifies_literal_evidence_and_rejects_changed_license():
    entry = sample_manifest()["sources"][0]["skills"][0]
    observed = refresh_catalog.validate_source(entry, source_text())
    assert observed["body_sha256"] == refresh_catalog.digest("Inspect before changing.\nDo not run downloaded programs.\n")
    with pytest.raises(ValueError, match="Evidence missing"):
        refresh_catalog.validate_source(entry, source_text().replace("Inspect before changing.", "Change immediately."))
    with pytest.raises(ValueError, match="license changed"):
        refresh_catalog.validate_source(entry, source_text().replace("license: MIT", "license: Proprietary"))


def test_refresh_only_requests_pinned_sources_and_regenerates_deterministically():
    manifest = sample_manifest()
    source = manifest["sources"][0]
    calls = []
    license_text = "MIT License\nCopyright Example\n"

    def fetch(url):
        calls.append(url)
        assert source["revision"] in url
        if "api.github.com" in url:
            return json.dumps({"tree": [
                {"path": "skills/example/SKILL.md", "type": "blob"},
                {"path": "skills/example/scripts/check.py", "type": "blob"},
                {"path": "skills/example/references/guide.md", "type": "blob"},
            ]})
        return source_text() if url.endswith("SKILL.md") else license_text

    first = refresh_catalog.refresh(deepcopy(manifest), fetch)
    second = refresh_catalog.refresh(deepcopy(first), fetch)
    assert first == second
    assert len(calls) == 5
    entry = first["sources"][0]["skills"][0]
    assert entry["inventory"] == {"helper_files": 1, "support_files": 1}
    skill = refresh_catalog.compile_snapshot(first)["skills"][0]
    assert skill["slug"] == "example"
    assert len([r for r in skill["requirements"] if r.startswith("Bundled support:")]) == 1


def test_truncated_tree_cannot_silently_drop_dependencies():
    with pytest.raises(ValueError, match="Truncated GitHub tree"):
        refresh_catalog.refresh(sample_manifest(), lambda url: json.dumps({"truncated": True}) if "api.github.com" in url else source_text())


def test_ids_survive_new_revision_and_distinct_methods_survive_same_task():
    manifest = sample_manifest()
    manifest["sources"][0]["inventory_revision"] = manifest["sources"][0]["revision"]
    original = refresh_catalog.compile_snapshot(manifest)["skills"][0]
    manifest["sources"][0]["revision"] = "a" * 40
    manifest["sources"][0]["inventory_revision"] = "a" * 40
    assert refresh_catalog.compile_snapshot(manifest)["skills"][0]["id"] == original["id"]
    entry = deepcopy(manifest["sources"][0]["skills"][0])
    entry.update(path="skills/different-method/SKILL.md", body_sha256="f" * 64)
    manifest["sources"][0]["skills"].append(entry)
    assert len(refresh_catalog.compile_snapshot(manifest)["skills"]) == 2
    entry["body_sha256"] = manifest["sources"][0]["skills"][0]["body_sha256"]
    with pytest.raises(ValueError, match="Exact duplicate instruction body"):
        refresh_catalog.compile_snapshot(manifest)
