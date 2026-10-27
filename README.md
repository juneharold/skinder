# Skinder

Skinder helps people find agent skills that fit a task and their working style. It imports public listings, searches skill descriptions and source files, and uses a language model to explain recommendations. It does not install or execute discovered skills.

The hosted app uses PostgreSQL with pgvector. The web process handles GitHub sign-in and the interface; a separate worker claims and processes matching jobs. A scheduled ingest process searches SkillsMP through its documented API for a configured set of keywords and builds its embedding index.

Each matching request has an idempotency key and a persisted job. Jobs capture the task, preferences, feedback, model, and prompt instructions when accepted. Workers use expiring leases and ownership tokens; restarting the API does not interrupt them. A worker failure before dispatch can be recovered. If the model may already have run, the job fails without an automatic second call. Cancellation prevents publication, but cannot guarantee that a remote model stopped or that its usage was refunded.

Usage reservations and reported costs are recorded transactionally. Unknown costs remain reserved for that UTC day's budget. Each account owns its preferences, feedback, and saved skills. Failed or cancelled additional batches preserve the last successful recommendations; independent saves survive feedback undo.

## Local development

Requirements: Docker Compose, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 22.12 or newer.

```sh
test -f .env || cp .env.example .env
```

For local use, the example file supplies the PostgreSQL URL and app origin. Fill in `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `SESSION_SECRET` for sign-in; set `LETSUR_API_KEY` to enable matching. Generate the session secret with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. In the GitHub OAuth app, set the callback URL to `http://127.0.0.1:8742/api/auth/callback`.

Start the database, build the application image, apply migrations, then run the web app and worker:

```sh
docker compose up -d postgres
docker compose build
docker compose run --rm migrate
docker compose up -d web worker
```

Open **http://127.0.0.1:8742**. PostgreSQL is published on `127.0.0.1:55433`; the web app is published on `127.0.0.1:8742`. Compose stores database data in its own `postgres-data` volume. This is separate from any SQLite files in the checkout.

The image includes the pinned E5-small model and verifies its files during image construction. The first build downloads about 470 MB. Runtime processes use only the bundled model and do not download it. To import or refresh the catalog and build its search index, run the manual Compose operation:

```sh
docker compose --profile ops run --rm ingest
```

Ingest uses the configured keyword list and resumes unfinished searches from durable page checkpoints. Completed keyword windows are searched again on a later run. It does not enumerate the full SkillsMP catalog. Each run is bounded by a request budget, and a persisted daily budget limits total calls across runs. A request-budget pause exits successfully with `pause_reason=budget`; the next run resumes unfinished searches. Provider failures exit with code 2 and `pause_reason=provider`. Compose does not run ingest automatically when the app starts.

For backend development without containers, install dependencies with `uv sync --frozen` and `npm ci --prefix frontend`. Run PostgreSQL with pgvector on port 55433 and set `DATABASE_URL` in `.env` to `postgresql+psycopg://skinder:skinder@127.0.0.1:55433/skinder`. Set `SKINDER_EMBEDDING_MODEL_DIR` to a writable local directory and prepare the pinned model explicitly:

```sh
uv run python -c "from skinder.retrieval import prepare_model; from skinder.settings import Settings; prepare_model(Settings().embedding_model_dir)"
uv run skinder-migrate
npm --prefix frontend run build
uv run skinder
```

Start the worker in another terminal with `uv run skinder-worker`. The web entrypoint serves the built React files and API from the same origin. Vite is also available with `npm --prefix frontend run dev` on port 5174; sign in through the backend origin first, then return to the Vite page.

## Configuration

`.env.example` lists all application settings and defaults. Copy it to `.env` for Compose or export equivalent variables in the service environment. Never put production credentials in the repository or in frontend variables such as `VITE_*`.

| Setting | Used by | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | All roles | PostgreSQL connection string. Compose overrides it to use its private `postgres` service. |
| `APP_ORIGIN` | Web | Public app origin, with no path. Use HTTPS in production. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Web | GitHub OAuth application credentials. Callback is `{APP_ORIGIN}/api/auth/callback`. |
| `SESSION_SECRET` | Web | At least 32 characters; signs the temporary OAuth state cookie. Login sessions use opaque database-backed tokens. |
| `LETSUR_API_KEY` | Worker | Server-side key for model requests. |
| `LETSUR_BASE_URL` | Worker | HTTPS model API endpoint. |
| `LETSUR_MODEL` | Web and worker | Model name, shared across services. Each accepted job retains its selected model. |
| `SKINDER_EMBEDDING_MODEL_DIR` | All roles | Bundled E5-small model path; image default is `/opt/skinder/models/e5-small`. |
| `SKINDER_SYNC_KEYWORDS` | Ingest | JSON array of SkillsMP search terms. Default: `["coding","testing","design","data","documentation","automation"]`. |
| `SKINDER_SYNC_MAX_REQUESTS` | Ingest | Maximum remote requests per run, including retries. Default: `12`. |
| `SKINDER_SYNC_DAILY_REQUESTS` | Ingest | Maximum remote requests per UTC day across runs. Default: `48`. |
| `SKINDER_SYNC_MIN_INTERVAL_SECONDS` | Ingest | Minimum seconds between requests. Default: `6`. |

Other `SKINDER_*` variables tune database pools, session expiry, model request limits, job budgets and timeouts, source fetching, and catalog sync. The example file documents their default values. Quotas reset at 00:00 UTC. A web service needs database, GitHub, and session credentials. A worker needs database and Letsur credentials. Ingest and migrations need no GitHub or model API credentials; indexing needs the prepared embedding files. Configuration is loaded once per process, so restart affected services after changes. Request logs contain route patterns, not query strings or submitted content.

## Deploy on Railway

Create four services in the project: three application services from the same GitHub repository, plus PostgreSQL. Configure each application service in Railway's dashboard. Railway has deprecated Config as Code. New services cannot opt in to these legacy files; existing legacy services are supported through December 1, 2026. See [Railway's Config as Code migration notice](https://docs.railway.com/config-as-code/reference).

For each application service, select the Dockerfile builder and set the Dockerfile path to `Dockerfile` at the repository root.

- **Web:** start command `skinder`; pre-deploy command `skinder-migrate`; healthcheck path `/health/ready`, timeout 300 seconds; restart on failure, up to 10 retries.
- **Worker:** start command `skinder-worker`; restart on failure, up to 10 retries.
- **Ingest:** start command `best-effort-full-sync`; schedule `0 */6 * * *`; restart policy `Never` (the cron schedule starts each run).

Provision PostgreSQL with `pgvector/pgvector:0.8.7-pg18-bookworm`. Attach persistent storage mounted at `/var/lib/postgresql` and set `PGDATA=/var/lib/postgresql/18/docker`. Keep the database private. Set its private `DATABASE_URL` on all three application services. The web pre-deploy migration creates the `vector` extension and application schema.

Set `SKINDER_ENV=production` and the public HTTPS `APP_ORIGIN` on the web service. Add the GitHub OAuth client ID and secret, and a unique `SESSION_SECRET` of at least 32 characters. Set `LETSUR_API_KEY` on the worker. Set matching `LETSUR_MODEL` values on web and worker. On the ingest service, set `SKINDER_SYNC_KEYWORDS` to a JSON array if you want different search terms; its default is a bounded starter set. The documented [SkillsMP search API](https://skillsmp.com/docs/api) allows anonymous keyword requests, so no SkillsMP API key is required. Its documented anonymous allowance is 50 requests per day. The app caps itself at 48 requests per day by default; other anonymous calls from the same IP may also use the provider allowance. This keyword search does not enumerate the full catalog and cannot guarantee complete coverage.

Set the GitHub OAuth callback URL to `{APP_ORIGIN}/api/auth/callback`, using the same public origin as the web service. Railway's pre-deploy command runs with that service's environment variables and private network access; it does not mount service volumes. The migration only needs `DATABASE_URL`. See [Railway's pre-deploy command guide](https://docs.railway.com/deployments/pre-deploy-command).

Check the database plan's backup options, confirm backups are enabled, and perform a restore exercise before relying on the deployment for durable user data. Availability depends on the selected plan; the application image and service filesystem are not database backups. Keep one web service and one or more workers within the configured concurrency and database capacity. Listings do not establish compatibility, license, or revision; these remain unknown without source evidence. Recommendations send the task, preferences, shortlisted descriptions, source excerpts, and relevant feedback to the configured model provider. Review that data flow and the provider's terms before inviting users.

## Checks

```sh
uv sync --frozen
uv run ruff check src tests
TEST_DATABASE_URL=postgresql+psycopg://skinder:skinder@127.0.0.1:55433/skinder uv run pytest -q
npm ci --prefix frontend
npm --prefix frontend test
npm --prefix frontend run build
```

Backend tests create isolated PostgreSQL schemas. They do not need live GitHub or model API credentials. The GitHub and model credentials are only needed to use those live services.
