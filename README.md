# Skinder

**Find your kind of skill.** A local app that matches your task and your taste with real agent skills for Codex and Claude Code.

Describe what you want to do, add optional working and output preferences, and review a deck of relevant skills. Pass, save, or explain why a skill does not fit. You can also save skills directly from the catalog or their details. Inspect the source and copy installation instructions when you find a good match.

## Run locally

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22.12+, and an installed [Codex CLI](https://learn.chatgpt.com/docs/cli) signed in with your ChatGPT account. macOS is the verified platform; the process management code also targets Linux.

```sh
codex login status
# If needed:
codex login

uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
uv run skinder
```

Open **http://127.0.0.1:8742**. These commands install locked dependencies, build the frontend, and run the Python backend. After the first build, start again with:

```sh
uv run skinder
```

A fresh catalog starts empty. Run `uv run best-effort-full-sync` in another terminal to import skills, as described below.

Catalog browsing, source inspection, and saved skills work without a Codex login. New recommendations use your existing Codex allowance. Skinder does not collect your login credentials or require an API key. Retrieval and embeddings run locally; matching sends only the shortlisted descriptions, source excerpts, task, preferences, and relevant feedback to Codex's model provider.

## Import SkillsMP and enable hybrid search

```sh
# Crawl the available catalog partitions, resume interrupted work, then build the local index.
uv run best-effort-full-sync

# Inspect progress without starting another crawl.
uv run best-effort-full-sync --status

# Rebuild search from already imported records, without contacting SkillsMP.
uv run best-effort-full-sync --index-only
```

The sync starts with public category, occupation, and language lists. It paginates each partition, splits capped searches using remaining filters, also checks recently updated results, and deduplicates skills by repository and path. It checkpoints progress, respects provider limits, and can take hours or longer. Rerunning resumes unfinished work; after a completed pass, the next invocation starts a new refresh. Existing skills and bookmarks are never removed just because a later crawl misses them.

This is **best-effort-full-sync**, not a guarantee that every SkillsMP listing was obtained. Provider caps, undiscovered taxonomy, changing results, and access failures remain visible in the report. A quota/access interruption pauses the crawl. Use `--max-requests 10` for a bounded trial; the budget includes taxonomy requests and retries. A paused run exits with code 2 after indexing available records; Ctrl-C checkpoints and exits with code 130 without starting indexing. Use `--catalog-db-path PATH` or `SKINDER_CATALOG_DB_PATH` to select a catalog database. The website listing endpoint currently permits 48 records per page, which differs from the documented REST search endpoint.

The first indexing run downloads the pinned multilingual E5-small model (about 470 MB). FastEmbed runs it locally, SQLite FTS5 supplies keyword rankings, and a persistent USearch index supplies vector candidates. Changed metadata is re-embedded; unchanged records reuse their vectors. A failed index build preserves the previous published index. Until an index is ready, matching uses keyword retrieval and the app identifies that reduced mode.

Each match combines keyword and vector rankings into at most 40 candidates. Imported skills' original `SKILL.md` files are fetched from GitHub only for that shortlist, cached for seven days, and supplied as bounded reference excerpts to Codex. Source failures preserve metadata matching and are identified as missing evidence. Imported compatibility, license, revision, and style remain unknown unless supported by evidence; importing a listing does not verify it. Codex chooses zero to twelve final recommendations. Nothing downloaded is installed or executed.

## What is included

- A persistent local catalog of skills imported from SkillsMP, with search and category filters.
- Separate explanations for task fit and working-style fit, with setup and style caveats when relevant.
- Swipe gestures, labeled pass/save buttons, keyboard controls while reviewing the deck (left/right arrows, Z to undo), and an optional reason for passing.
- Bookmarks from the deck, catalog, or skill details, with search, category filters, source evidence, and copyable installation instructions.
- Persistent preferences and progress. Refreshing restores the latest task and the view and filters in the URL; browser Back and Forward restore navigation.

Task relevance comes first. Explicit preferences take priority over learned hints. A plain pass or “not this task” only dismisses the skill for the current task; “not my style” and “too much setup” can inform later matches. Saves from the deck are weak, contextual preference signals. Direct catalog and detail bookmarks do not create preference feedback. Feedback changes the next batch, not the deck you are currently reviewing.

Editing a task starts a new deck; finding more continues the task shown above the current deck. Undo reverses the latest choice in the current batch and preserves skills saved independently. Passing a skill never removes an existing bookmark.

Style descriptions are interpretations of source instructions, not measured performance or guarantees about output. Some skills require additional tools or a companion suite; their details describe those requirements. Skinder copies installation instructions and never installs or executes a discovered skill.

## Development

After installing dependencies as above, start the backend from the project root:

```sh
uv run uvicorn skinder.app:create_app --factory --host 127.0.0.1 --port 8742 --reload
```

In another terminal, start the frontend:

```sh
npm --prefix frontend run dev
```

The React frontend runs at **http://127.0.0.1:5174**, with API requests proxied to FastAPI on port 8742. The backend reloads when Python files change. Do not run the production and development backends on the same port at once.

The backend uses FastAPI, Pydantic, SQLAlchemy ORM, Alembic, and an asynchronous Codex subprocess. SQLite remains the database engine. The frontend uses React, TypeScript, Vite, and locally bundled fonts.

`MatchService` in `src/skinder/matching.py` owns matching, cancellation, result validation, and shutdown. FastAPI handlers delegate the workflow to that service. Synchronous database operations run in worker threads, with each operation owning its database session and transaction. User-data writes reserve the SQLite writer before reading records they may change, so concurrent requests preserve feedback and bookmark invariants.

Database records are mapped in `src/skinder/orm.py`; Pydantic models remain the API contract. `UserStore` in `src/skinder/user.py` manages user data, and `CatalogStore` in `src/skinder/catalog.py` manages catalog data. Both use `db_sessions` to create SQLAlchemy database sessions. Constructing a store does not recover interrupted matching; application startup performs recovery after acquiring exclusive ownership of the user database. Alembic owns schema creation, indexes, and triggers, with separate migration histories for the user and catalog databases. Startup and catalog commands automatically apply pending migrations. SQLite FTS5 search uses SQLAlchemy expressions and parameterized SQL for its virtual table.

On the frontend, `App.tsx` composes navigation, preferences, and dialogs. Hooks in `frontend/src/hooks/` own matching and polling, saved skills, catalog pagination, and skill hydration. `DiscoverView` and `LibraryView` render those workflows through typed data and actions. Request guards and version checks keep late responses from replacing newer user choices.

To manage migrations explicitly:

```sh
uv run alembic -n user upgrade head
uv run alembic -n catalog upgrade head
uv run alembic -n user check
uv run alembic -n catalog check

# After changing ORM models, generate and review a migration for the affected database.
uv run alembic -n user revision --autogenerate -m "Describe user data change"
uv run alembic -n catalog revision --autogenerate -m "Describe catalog change"
```

`SKINDER_USER_DB_PATH` selects the user database and `SKINDER_CATALOG_DB_PATH` selects the catalog for these commands. Migration files live under `src/skinder/migrations/user/versions` and `src/skinder/migrations/catalog/versions`. Development uses a fresh user database with the current migration baseline; older user databases are not migrated.

Pydantic models define the API contract. FastAPI serves interactive documentation at **http://127.0.0.1:8742/docs** and the schema at **http://127.0.0.1:8742/openapi.json**. After changing the models, restart the backend (or use development reload), then regenerate the frontend types from the running backend:

```sh
npm --prefix frontend run api:types
```

## Checks

```sh
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Backend tests use real temporary SQLite databases and subprocesses that simulate the external Codex boundary. Automated checks do not consume model usage. Frontend tests exercise user interactions.

The historical [hybrid retrieval results](evaluation/hybrid-retrieval-results.json) record a bounded live import of 96 distinct SkillsMP entries, cached source retrieval, a real Codex recommendation, and a 100,000-vector synthetic benchmark. That run also included the former bundled catalog. It validates the implemented flow, not complete provider coverage or general recommendation quality.

## Data and boundaries

Skinder uses two separate SQLite database files, each with multiple tables:

| Database | Default file | Path setting | Contents |
| --- | --- | --- | --- |
| User | `.data/user.sqlite3` | `SKINDER_USER_DB_PATH` | Profile, matching sessions, feedback, and bookmarks |
| Catalog | `.data/catalog.sqlite3` | `SKINDER_CATALOG_DB_PATH` | Skills, cached sources, sync progress, and search embeddings |

Both files are ignored by Git. The application defaults to `catalog.sqlite3` beside the selected user database. Catalog commands use `.data/catalog.sqlite3` by default; set `SKINDER_CATALOG_DB_PATH` when they should use another location. CLI imports never open the user database. New catalogs are populated by SkillsMP imports; existing catalog records and bookmarks are preserved. Search indexes and downloaded models are local artifacts beside the catalog database. No application telemetry is sent.

One matching request runs at a time, with cancellation and a three-minute timeout covering retrieval, source enrichment, and Codex. If cancellation or the timeout occurs during synchronous database or retrieval work, cleanup waits for that operation to finish before another match can start or the stores can close. A server restart marks unfinished matching as interrupted rather than automatically retrying. A lock beside the resolved user database prevents a second server from opening the same user database for recovery and matching; run one server process per user database. A failed additional batch preserves earlier results. Codex receives only the bounded shortlist in a temporary, read-only workspace with tools and integrations disabled. App startup never downloads an embedding model or contacts SkillsMP.

Unexpected matching failures log the session ID, processing stage, exception class, and stack locations. These diagnostics omit exception messages, source excerpts, task text, provider output, and credentials; the API retains a generic error message.

The app binds to loopback and rejects foreign hosts and browser origins. It is intended for personal localhost use. Accounts, hosted deployment, automatic installation, and task execution are outside this version.
