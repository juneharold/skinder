import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from skinder.app import create_app
from skinder.catalog import CatalogStore
from skinder.claude_matcher import ClaudeMatcher
from skinder.matcher import CodexMatcher
from skinder.models import Skill


@pytest.fixture
def backend(tmp_path, monkeypatch):
    skills = []
    for number in range(16):
        skills.append(
            {
                "id": f"skill-{number}",
                "name": f"Skill {number}",
                "slug": f"skill-{number}",
                "description": "Review changes and produce a concise, verified report.",
                "category": "Development",
                "author": "Example",
                "repository": "example/skills",
                "source_url": f"https://github.com/example/skills/blob/{'a' * 40}/SKILL.md",
                "revision": "a" * 40,
                "license": "MIT",
                "compatibility": ["claude-code"] if number == 15 else ["codex", "claude-code"],
                "requirements": [],
                "tags": ["review", "testing"],
                "workflow": ["Verify changes before reporting."],
                "output_style": ["Concise report."],
                "evidence": [{"trait": "verification", "excerpt": "Verify the change."}],
                "install_commands": {"codex": "echo copy-only", "claude-code": "echo copy-only"},
            }
        )
    catalog_db_path = tmp_path / "catalog.sqlite3"
    catalog = CatalogStore(catalog_db_path)
    for skill in skills:
        catalog.upsert(Skill.model_validate(skill))
    control = tmp_path / "control.json"
    control.write_text("{}")
    # A real child exercises signals and cleanup without invoking Codex or consuming model usage.
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["SKINDER_FAKE_CODEX"])
config = json.loads((root / "control.json").read_text())
if sys.argv[1:3] == ["login", "status"]:
    sys.exit(config.get("login_exit", 0))
request = json.load(sys.stdin)
(root / "request.json").write_text(json.dumps(request))
(root / "args.json").write_text(json.dumps(sys.argv[1:]))
(root / "pid").write_text(str(os.getpid()))
if config.get("sleep"):
    time.sleep(config["sleep"])
if config.get("exit"):
    print("access_token=secret-that-must-not-leak", file=sys.stderr)
    sys.exit(config["exit"])
result = config.get("result", {"recommendations": [
    {"skill_id": s["id"], "task_fit": "Reviews the requested code changes.",
     "taste_fit": "Produces the concise report you prefer.", "caveat": None}
    for s in request["eligible_skills"][:3]
]})
pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(
    result if isinstance(result, str) else json.dumps(result)
)
"""
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SKINDER_FAKE_CODEX", str(tmp_path))
    claude = tmp_path / "claude"
    claude.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["SKINDER_FAKE_CODEX"])
config = json.loads((root / "claude-control.json").read_text())
(root / "claude-check-env.json").write_text(json.dumps(dict(os.environ)))
if sys.argv[1:] == ["--help"]:
    print(config.get("help", "--print --output-format --json-schema --tools --strict-mcp-config "
        "--mcp-config --setting-sources --settings --system-prompt-file --no-session-persistence "
        "--disable-slash-commands --safe-mode --no-chrome"))
    sys.exit(0)
if "auth" in sys.argv and "status" in sys.argv:
    (root / "claude-auth-args.json").write_text(json.dumps(sys.argv[1:]))
    print(config.get("auth_raw", json.dumps(config.get("auth", {"loggedIn": True}))))
    sys.exit(config.get("login_exit", 0))
request = json.load(sys.stdin)
(root / "claude-request.json").write_text(json.dumps(request))
(root / "claude-args.json").write_text(json.dumps(sys.argv[1:]))
(root / "claude-env.json").write_text(json.dumps(dict(os.environ)))
(root / "claude-cwd").write_text(os.getcwd())
if "--system-prompt-file" in sys.argv:
    instructions = pathlib.Path(sys.argv[sys.argv.index("--system-prompt-file") + 1])
    (root / "claude-instructions.txt").write_text(instructions.read_text())
(root / "claude-pid").write_text(str(os.getpid()))
if config.get("sleep"):
    time.sleep(config["sleep"])
if config.get("exit"):
    print("access_token=secret-that-must-not-leak", file=sys.stderr)
    sys.exit(config["exit"])
result = config.get("result", {"recommendations": [
    {"skill_id": s["id"], "task_fit": "Reviews the requested code changes.",
     "taste_fit": "Produces the concise report you prefer.", "caveat": None}
    for s in request["eligible_skills"][:3]
]})
envelope = config.get("envelope", {
    "type": "result", "subtype": "success", "is_error": False, "structured_output": result,
})
if "raw" in config:
    print(config["raw"])
else:
    print(json.dumps(envelope))
"""
    )
    claude.chmod(0o755)
    claude_control = tmp_path / "claude-control.json"
    claude_control.write_text("{}")
    monkeypatch.setattr(
        "skinder.matcher.shutil.which",
        lambda name: {"codex": str(fake), "claude": str(claude)}.get(name),
    )
    user_db_path = tmp_path / "user.sqlite3"

    class SessionTestRetriever:
        # Session/feedback tests control ranking; real hybrid behavior has its own tests.
        def search(self, task, agent, excluded):
            return [Skill.model_validate(skill) for skill in skills
                    if skill["id"] not in excluded and agent in skill["compatibility"]][:40]

        def status(self):
            return {"indexed_count": 0, "retrieval_mode": "keyword-only"}

    @asynccontextmanager
    async def client(timeout=180, frontend_path=None, retriever=None, enricher=None):
        # Reopening the same files with a fresh app lets restart tests exercise persisted recovery.
        app = create_app(
            user_db_path=user_db_path,
            catalog_db_path=catalog_db_path,
            matchers={"codex": CodexMatcher(timeout=timeout),
                      "claude-code": ClaudeMatcher(timeout=timeout)},
            frontend_path=frontend_path,
            retriever=retriever or SessionTestRetriever(),
            enricher=enricher,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742"
            ) as connection,
        ):
            yield connection

    def configure(engine="codex", **config):
        selected = control if engine == "codex" else claude_control
        selected.write_text(json.dumps(config))

    return SimpleNamespace(
        client=client, configure=configure, root=tmp_path, user_db_path=user_db_path, skills=skills
    )
