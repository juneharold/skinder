"""Opt-in authored-example evaluation using the real API, Codex, and temporary state.

Run: uv run python scripts/evaluate_matches.py --live
An existing Codex login is required. This makes up to six model calls.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from skinder.app import create_app
from skinder.models import MatchSession

ROOT = Path(__file__).resolve().parents[1]


async def evaluate(args):
    cases = json.loads((ROOT / "evaluation" / "cases.json").read_text(encoding="utf-8"))
    if not 1 <= len(cases) <= 6:
        raise ValueError("This evaluation permits one to six authored cases.")
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            raise ValueError(f"Unknown case: {args.case}")
    catalog_bytes = args.catalog.read_bytes()
    catalog = json.loads(catalog_bytes)
    skills = {skill["id"]: skill for skill in catalog["skills"]}
    for case in cases:
        missing = set(case["acceptable_top3_slugs"]) - {skill["slug"] for skill in skills.values()}
        if missing:
            raise ValueError(f"Expected skills absent from catalog: {sorted(missing)}")
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "evaluation_type": "Six developer-authored examples; not an independent benchmark.",
        "catalog_count": len(skills),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "catalog_generated_at": catalog.get("generated_at"),
        "model": "Codex CLI default with --ignore-user-config; exact model not observed.",
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="skinder-evaluation-") as directory:
        app = create_app(db_path=Path(directory) / "state.sqlite3", catalog_path=args.catalog)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8742"
            ) as client,
        ):
            health_response = await client.get("/api/health")
            health_response.raise_for_status()
            health = health_response.json()
            if not health["codex"]["authenticated"]:
                raise RuntimeError(health["codex"]["message"])
            version = await asyncio.to_thread(
                subprocess.run,
                ["codex", "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            report["codex_version"] = version.stdout.strip()
            for case in cases:
                profile_response = await client.put("/api/profile", json=case["profile"])
                profile_response.raise_for_status()
                started = time.monotonic()
                response = await client.post(
                    "/api/matches", json={"task": case["task"], "agent": case["profile"]["agent"]}
                )
                response.raise_for_status()
                session = response.json()
                while session["status"] in ("running", "pending"):
                    if time.monotonic() - started > 185:
                        await client.post(f"/api/matches/{session['id']}/cancel")
                        raise TimeoutError(f"Case {case['id']} exceeded the matcher timeout.")
                    await asyncio.sleep(0.25)
                    response = await client.get(f"/api/matches/{session['id']}")
                    response.raise_for_status()
                    session = response.json()
                MatchSession.model_validate(session, strict=True)
                slugs = [skills[item["skill_id"]]["slug"] for item in session["recommendations"]]
                acceptable = set(case["acceptable_top3_slugs"])
                record = {
                    "case": case,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "schema_valid": True,
                    "returned_slugs": slugs,
                    "top3_acceptable_count": sum(slug in acceptable for slug in slugs[:3]),
                    "expectation_met": session["status"] == "completed"
                    and (not slugs if case["expect_empty"] else bool(set(slugs[:3]) & acceptable)),
                    "session": session,
                }
                report["cases"].append(record)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                print(
                    f"{case['id']}: {session['status']}, {record['elapsed_seconds']}s, "
                    f"top 3: {', '.join(slugs[:3]) or '(none)'}",
                    flush=True,
                )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize up to six real model calls.")
    parser.add_argument("--case", help="Run only this authored case, for an explicit retry.")
    parser.add_argument(
        "--catalog", type=Path, default=ROOT / "src" / "skinder" / "data" / "catalog.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation" / "results.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run real Codex matching. No inference was started.")
    report = asyncio.run(evaluate(args))
    if not all(case["expectation_met"] for case in report["cases"]):
        raise SystemExit("Some authored expectations were not met; inspect the saved results.")


if __name__ == "__main__":
    main()
