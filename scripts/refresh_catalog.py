#!/usr/bin/env python3
"""Validate the offline snapshot, or explicitly retrieve its pinned sources.

    python scripts/refresh_catalog.py --check       # offline; also the default
    python scripts/refresh_catalog.py --refresh     # network; never runs source code

To adopt an upstream revision, edit that repository's SHA in sources.json,
review the selected instructions and metadata, then run --refresh. Changed
literal evidence fails closed so stale editorial claims cannot silently ship.
The manifest owns selection and editorial metadata; GitHub owns frontmatter,
body digests, inventories, and license text. IDs exclude the revision.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "skinder" / "data"
CATEGORIES = {"Development", "Design", "Writing", "Research", "Documents", "Data", "Productivity", "Marketing"}
AGENTS = {"codex", "claude-code"}
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".ps1"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(repository: str, path: str) -> str:
    return digest(repository.lower() + ":" + str(PurePosixPath(path).parent))[:20]


def parse_skill(text: str) -> tuple[dict[str, str], str]:
    """Read top-level scalar frontmatter without evaluating YAML or source code.

    Skill metadata uses plain, quoted, folded, or literal strings. Nested YAML
    metadata is ignored. This deliberately isn't a general YAML loader.
    """
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)\Z", text, re.S)
    if not match:
        raise ValueError("SKILL.md must begin with delimited frontmatter")
    fields: dict[str, str] = {}
    lines = match[1].splitlines()
    index = 0
    while index < len(lines):
        item = re.match(r"^([a-zA-Z][\w-]*):\s*(.*)$", lines[index])
        index += 1
        if not item:
            continue
        key, raw = item.groups()
        continuation = []
        while index < len(lines) and (not lines[index] or lines[index][0].isspace()):
            continuation.append(lines[index].strip())
            index += 1
        if key not in {"name", "description", "license", "compatibility"}:
            continue
        if raw in {">", ">-", ">+", "|", "|-", "|+"}:
            value = ("\n" if raw.startswith("|") else " ").join(continuation)
        else:
            value = " ".join([raw, *continuation]).strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
        fields[key] = value
    if not fields.get("name") or not fields.get("description") or not match[2].strip():
        raise ValueError("SKILL.md requires name, description, and an instruction body")
    return fields, match[2]


def validate_source(entry: dict, text: str) -> dict:
    frontmatter, body = parse_skill(text)
    if entry.get("slug") and frontmatter["name"] != entry["slug"]:
        raise ValueError(f"Frontmatter name changed for {entry['path']}; review the manifest")
    for source_key, recorded_key in (("license", "declared_license"), ("compatibility", "source_compatibility")):
        if recorded_key in entry and frontmatter.get(source_key, "") != entry[recorded_key]:
            raise ValueError(f"Source {source_key} changed for {entry['path']}; review its catalog metadata")
    for evidence in entry["evidence"]:
        if evidence["excerpt"] not in body:
            raise ValueError(f"Evidence missing from {entry['path']}: {evidence['excerpt']!r}")
    return {"slug": frontmatter["name"], "source_sha256": digest(text), "body_sha256": digest(body)}


def compile_snapshot(manifest: dict) -> dict:
    skills = []
    bodies: set[str] = set()
    for source in manifest["sources"]:
        repository, revision = source["repository"], source["revision"]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Each source needs an owner/repository and full pinned commit SHA")
        if source.get("inventory_revision") != revision:
            raise ValueError(f"Source inventory needs refresh: {repository}")
        for entry in source["skills"]:
            path = entry["path"]
            parts = PurePosixPath(path).parts
            if path.startswith("/") or ".." in parts or not path.endswith("/SKILL.md"):
                raise ValueError(f"Invalid source path: {path}")
            if not re.fullmatch(r"[a-f0-9]{64}", entry["body_sha256"]):
                raise ValueError(f"Missing body digest: {path}")
            if entry["body_sha256"] in bodies:
                raise ValueError(f"Exact duplicate instruction body: {path}")
            bodies.add(entry["body_sha256"])
            skill = {key: entry[key] for key in ("name", "slug", "description", "category", "license", "compatibility", "requirements", "tags", "workflow", "output_style", "evidence")}
            skill.update(id=stable_id(repository, path), author=repository.split("/")[0], repository=repository,
                         source_url=f"https://github.com/{repository}/blob/{revision}/{path}", revision=revision)
            skill["install_commands"] = {}
            if entry["installation"] == "standalone":
                target = f"https://github.com/{repository}/tree/{revision}/{PurePosixPath(path).parent}"
                skill["install_commands"] = {agent: f"npx skills add {target} --agent {agent}" for agent in entry["compatibility"]}
            elif entry["installation"] != "source-setup":
                raise ValueError(f"Unknown installation method: {path}")
            validate_skill(skill)
            skills.append(skill)
    if len({s["id"] for s in skills}) != len(skills):
        raise ValueError("Duplicate source directory or stable ID")
    return {"version": 1, "generated_at": manifest["generated_at"], "skills": sorted(skills, key=lambda s: (s["category"], s["name"].casefold(), s["id"]))}


def validate_skill(skill: dict) -> None:
    if skill["category"] not in CATEGORIES or not 0 < len(skill["description"]) <= 300:
        raise ValueError(f"Invalid category or description: {skill['name']}")
    if not skill["compatibility"] or not set(skill["compatibility"]) <= AGENTS:
        raise ValueError(f"Unsupported agent: {skill['name']}")
    if not (1 <= len(skill["workflow"]) <= 3 and len(skill["output_style"]) <= 3 and 2 <= len(skill["tags"]) <= 5):
        raise ValueError(f"Invalid trait/tag counts: {skill['name']}")
    for key in ("requirements", "tags", "workflow", "output_style", "compatibility"):
        if not isinstance(skill[key], list) or any(not isinstance(v, str) or not v.strip() for v in skill[key]):
            raise ValueError(f"Invalid {key}: {skill['name']}")
    if not all(skill[key].strip() for key in ("name", "slug", "author", "repository", "license")):
        raise ValueError("Missing catalog metadata")
    traits = skill["workflow"] + skill["output_style"]
    if {e["trait"] for e in skill["evidence"]} != set(traits):
        raise ValueError(f"Every inferred trait needs source evidence: {skill['name']}")
    if any(not e["excerpt"] or len(e["excerpt"].split()) > 14 for e in skill["evidence"]):
        raise ValueError(f"Evidence must be a short literal excerpt: {skill['name']}")
    if sum(len(e["excerpt"].split()) for e in skill["evidence"]) > 25:
        raise ValueError(f"Evidence exceeds per-skill quotation limit: {skill['name']}")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Skinder-catalog-maintenance/1.0"})
    with urlopen(request, timeout=60) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("Source exceeds the maintenance download limit")
    return payload.decode("utf-8-sig")


def refresh(manifest: dict, fetch=fetch_text) -> dict:
    """Fetch pinned files and metadata only. Nothing downloaded is executed."""
    jobs = []
    for source in manifest["sources"]:
        base = f"https://raw.githubusercontent.com/{source['repository']}/{source['revision']}/"
        if source.get("inventory_revision") != source["revision"]:
            jobs.append((source, "tree", f"https://api.github.com/repos/{source['repository']}/git/trees/{source['revision']}?recursive=1"))
        jobs.extend((source, entry, base + entry["path"]) for entry in source["skills"])
        jobs.extend((source, license_entry, base + license_entry["path"]) for license_entry in source["licenses"])
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda job: fetch(job[2]), jobs))
    trees = {}
    for (source, target, _url), text in zip(jobs, results):
        if target == "tree":
            tree = json.loads(text)
            if tree.get("truncated"):
                raise ValueError("Truncated GitHub tree; refuse an incomplete source inventory")
            trees[source["repository"]] = tree["tree"]
        elif "evidence" in target:
            target.update(validate_source(target, text))
        else:
            key = digest(text)
            manifest["license_texts"][key] = text
            target["sha256"] = key
    for source in manifest["sources"]:
        for entry in source["skills"]:
            directory = str(PurePosixPath(entry["path"]).parent) + "/"
            if source["repository"] in trees:
                entry["inventory_paths"] = [node["path"] for node in trees[source["repository"]]
                                            if node["type"] == "blob" and node["path"].startswith(directory)]
            files = entry["inventory_paths"]
            if entry["path"] not in files:
                raise ValueError(f"Selected skill absent from pinned tree: {entry['path']}")
            entry["inventory"] = {"helper_files": sum(PurePosixPath(p).suffix in CODE_SUFFIXES for p in files), "support_files": sum(PurePosixPath(p).suffix not in CODE_SUFFIXES and p != entry["path"] for p in files)}
            requirements = [r for r in entry["requirements"] if not r.startswith("Bundled support:")]
            inventory = entry["inventory"]
            if inventory["helper_files"]:
                requirements.append(f"Bundled support: {inventory['helper_files']} executable/source helper files and {inventory['support_files']} other files; review their setup before use.")
            entry["requirements"] = requirements
        source["inventory_revision"] = source["revision"]
    used = {item["sha256"] for source in manifest["sources"] for item in source["licenses"]}
    manifest["license_texts"] = {key: value for key, value in manifest["license_texts"].items() if key in used}
    compile_snapshot(manifest)
    return manifest


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument("--refresh", action="store_true", help="retrieve only the pinned source files, inventories and licenses")
    flags.add_argument("--check", action="store_true", help="check the shipped snapshot offline (default)")
    args = parser.parse_args()
    manifest = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    if args.refresh:
        manifest = refresh(manifest)
        snapshot = compile_snapshot(manifest)
        write_json(DATA / "sources.json", manifest)
        write_json(DATA / "catalog.json", snapshot)
    else:
        snapshot = compile_snapshot(manifest)
        if snapshot != json.loads((DATA / "catalog.json").read_text(encoding="utf-8")):
            raise ValueError("Snapshot differs from the source manifest; review metadata then run --refresh")
    print(f"Validated {len(snapshot['skills'])} skills from {len(manifest['sources'])} pinned sources.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"Catalog maintenance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
