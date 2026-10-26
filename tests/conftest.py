import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from skinder.app import create_app
from skinder.matcher import CodexMatcher


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
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"version": 1, "skills": skills}))
    control = tmp_path / "control.json"
    control.write_text("{}")
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
    monkeypatch.setattr("skinder.matcher.shutil.which", lambda name: str(fake))
    db_path = tmp_path / "state.sqlite3"

    @asynccontextmanager
    async def client(timeout=180, frontend_path=None):
        app = create_app(
            db_path=db_path,
            catalog_path=catalog_path,
            matcher=CodexMatcher(timeout=timeout),
            frontend_path=frontend_path,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742"
            ) as connection,
        ):
            yield connection

    def configure(**config):
        control.write_text(json.dumps(config))

    return SimpleNamespace(
        client=client, configure=configure, root=tmp_path, db_path=db_path, skills=skills
    )
