# Skinder

**Find your kind of skill.** A local app that matches your task and your taste with real agent skills for Codex and Claude Code.

Describe what you want to do, add optional working and output preferences, and review a deck of relevant skills. Pass, save, or explain why a skill does not fit. You can also save skills directly from the catalog or their details. Inspect the source and copy installation instructions when you find a good match.

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

This is **best-effort-full-sync**, not a guarantee that every SkillsMP listing was obtained. Provider caps, undiscovered taxonomy, changing results, and access failures remain visible in the report. A quota/access interruption pauses the crawl. Use `--max-requests 10` for a bounded trial; the budget includes taxonomy requests and retries. A paused run exits with code 2 after indexing available records; Ctrl-C checkpoints and exits with code 130 without starting indexing. Use `--db PATH` or `SKINDER_CATALOG_DB_PATH` to select a catalog database. The website listing endpoint currently permits 48 records per page, which differs from the documented REST search endpoint.

The first indexing run downloads the pinned multilingual E5-small model (about 470 MB). FastEmbed runs it locally, SQLite FTS5 supplies keyword rankings, and a persistent USearch index supplies vector candidates. Changed metadata is re-embedded; unchanged records reuse their vectors. A failed index build preserves the previous published index. Until an index is ready, matching uses keyword retrieval and the app identifies that reduced mode.

Each match combines keyword and vector rankings into at most 40 candidates. Imported skills' original `SKILL.md` files are fetched from GitHub only for that shortlist, cached for seven days, and supplied as bounded reference excerpts to Codex. Source failures preserve metadata matching and are identified as missing evidence. Imported compatibility, license, revision, and style remain unknown unless supported by evidence; importing a listing does not verify it. Codex chooses zero to twelve final recommendations. Nothing downloaded is installed or executed.

## What is included

- A pinned catalog of real skills from multiple maintainers, across development, design, writing, research, documents, data, productivity, and marketing.
- Separate explanations for task fit and working-style fit, with setup and style caveats when relevant.
- Swipe gestures, labeled pass/save buttons, keyboard controls while reviewing the deck (left/right arrows, Z to undo), and an optional reason for passing.
- Bookmarks from the deck, catalog, or skill details, with search, category filters, source evidence, and copyable installation instructions.
- Persistent preferences and progress. Refreshing restores the latest task and the view and filters in the URL; browser Back and Forward restore navigation.

Task relevance comes first. Explicit preferences take priority over learned hints. A plain pass or “not this task” only dismisses the skill for the current task; “not my style” and “too much setup” can inform later matches. Saves from the deck are weak, contextual preference signals. Direct catalog and detail bookmarks do not create preference feedback. Feedback changes the next batch, not the deck you are currently reviewing.

Editing a task starts a new deck; finding more continues the task shown above the current deck. Undo reverses the latest choice in the current batch and preserves skills saved independently. Passing a skill never removes an existing bookmark.

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

The [hybrid retrieval results](evaluation/hybrid-retrieval-results.json) record a bounded live import of 96 distinct SkillsMP entries, cached source retrieval, a real Codex recommendation, and a 100,000-vector synthetic benchmark. This validates the implemented flow, not complete provider coverage or general recommendation quality. To repeat the index benchmark without model or provider calls:

```sh
uv run python scripts/benchmark_retrieval.py --count 100000 --queries 100 --output .data/retrieval-benchmark.json
```

The [live evaluation report](evaluation/REPORT.md) records six authored scenarios, their actual results, a no-match timeout that led to a prompt correction, and the successful checks afterward. These examples are not an independent benchmark. To repeat them without replacing the recorded results:

```sh
uv run python scripts/evaluate_matches.py --live --output .data/evaluation-results.json
```

## Data and boundaries

Personal state lives in `.data/skinder.sqlite3`, which is ignored by Git. Set `SKINDER_DB_PATH` to use another database. Catalog data lives in the adjacent `catalog.sqlite3` by default; set `SKINDER_CATALOG_DB_PATH` to override it. CLI imports never open the personal-state database. The 150 bundled skills seed the catalog without a network request. Search indexes and downloaded models are local artifacts beside the catalog database. No application telemetry is sent.

One matching request runs at a time, with cancellation and a three-minute timeout covering retrieval, source enrichment, and Codex. A server restart marks unfinished matching as interrupted rather than automatically retrying. A failed additional batch preserves earlier results. Codex receives only the bounded shortlist in a temporary, read-only workspace with tools and integrations disabled. App startup never downloads an embedding model or contacts SkillsMP.

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
