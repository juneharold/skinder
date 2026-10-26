# Skinder

**Find your kind of skill.** A local app that matches your task and your taste with real agent skills for Codex and Claude Code.

Describe what you want to do, add optional working and output preferences, and meet a deck of relevant skills. Pass, save, or explain what did not click. Inspect the source and copy installation instructions when you find a good match.

## Run locally

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22.12+, and an installed [Codex CLI](https://learn.chatgpt.com/docs/cli) signed in with your ChatGPT account. macOS is the verified platform; the process management code also targets Linux.

```sh
codex login status
# If needed:
codex login

bash scripts/start.sh
```

Open **http://127.0.0.1:8742**. The start script installs locked dependencies, builds the frontend, and runs the Python backend. After the first build, start again with:

```sh
uv run skinder
```

Catalog browsing, source inspection, and saved skills work without a Codex login. New recommendations use your existing Codex allowance. Skinder does not collect your login credentials or require an API key. The app runs locally, but matching sends the task, preferences, catalog descriptions, and relevant feedback to Codex's model provider.

## What is included

- A pinned catalog of real skills from multiple maintainers, across development, design, writing, research, documents, data, productivity, and marketing.
- Separate explanations for task fit and working-style fit, with setup and style caveats when relevant.
- Swipe gestures, pass/save buttons, keyboard controls (left/right arrows, Z to undo), and an optional reason for passing.
- Saved skills, search and category filters, source evidence, and copyable installation instructions.
- Persistent preferences and progress. Refreshing the page returns to the latest task.

Task relevance comes first. Explicit preferences take priority over learned hints. A plain pass or “not this task” only dismisses the skill for the current task; “not my style” and “too much setup” can inform later matches. Saves are weak, contextual preference signals. Feedback changes the next batch, not the deck you are currently reviewing.

Style descriptions are interpretations of source instructions, not measured performance or guarantees about output. Some skills require additional tools or a companion suite; their details describe those requirements. Skinder copies installation instructions and never installs or executes a discovered skill.

## Development

```sh
bash scripts/dev.sh
```

The React frontend runs at **http://127.0.0.1:5174**, with API requests proxied to FastAPI on port 8742. The backend reloads when Python files change. Do not run the production and development backends on the same port at once.

The backend uses FastAPI, Pydantic, Python's `sqlite3`, and an asynchronous Codex subprocess. The frontend uses React, TypeScript, Vite, and locally bundled fonts. There is no runtime dependency on Agentinder.

Pydantic models define the API contract. After changing them, regenerate the frontend types:

```sh
uv run python scripts/export_openapi.py docs/openapi.json
npm --prefix frontend run api:types
```

## Checks

```sh
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Backend tests use real temporary SQLite databases and subprocesses that simulate the external Codex boundary. Automated checks do not consume model usage. Frontend tests exercise user interactions. Live evaluation is separate and uses your signed-in Codex allowance.

The [live evaluation report](evaluation/REPORT.md) records six authored scenarios, their actual results, a no-match timeout that led to a prompt correction, and the successful checks afterward. These examples are not an independent benchmark. To repeat them without replacing the recorded results:

```sh
uv run python scripts/evaluate_matches.py --live --output .data/evaluation-results.json
```

## Data and boundaries

Local state lives in `.data/skinder.sqlite3`, which is ignored by Git. Set `SKINDER_DB_PATH` to use another database. The bundled catalog is available without network requests at startup. No application telemetry is sent.

One matching request runs at a time, with cancellation and a three-minute timeout. A server restart marks unfinished matching as interrupted rather than automatically retrying. A failed additional batch preserves earlier results. Matching uses only supplied descriptions in a temporary, read-only workspace with tools and integrations disabled.

The app binds to loopback and rejects foreign hosts and browser origins. It is intended for personal localhost use. Accounts, hosted deployment, automatic installation, and task execution are outside this version.

## Catalog maintenance

The snapshot contains **150 skills from seven maintainers**. Source revisions, selected paths, editorial descriptions, evidence, and available license notices are recorded in `src/skinder/data/sources.json`. There are 135 direct installation commands; fifteen skills with companion dependencies instead point to the author's setup instructions. Compatibility is based on reviewing the instructions, not executing every skill.

```sh
# Validate the checked-in snapshot without network access.
uv run python scripts/refresh_catalog.py --check

# Explicitly refetch the recorded source revisions and verify evidence.
uv run python scripts/refresh_catalog.py --refresh
```

To adopt a new upstream revision, update its commit in the source manifest and review the selected skills before refreshing. Changed evidence or license declarations fail validation. A refresh never installs or executes source code, and it never changes the selected revisions automatically.
