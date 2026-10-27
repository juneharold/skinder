# Skinder

Skinder helps people find agent skills that fit a task and their working style. It imports public listings, searches skill descriptions and source files, and uses a language model to explain recommendations. It does not install or execute discovered skills.

The hosted app uses PostgreSQL with pgvector. The web process handles GitHub sign-in and the interface; a separate worker claims and processes matching jobs. A one-off ingest process imports the catalog and builds its embedding index.

## Local development

Requirements: Docker Compose, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 22.12 or newer.

```sh
cp .env.example .env
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

Ingest resumes interrupted imports. It can take hours, depending on provider responses. Compose does not run ingest automatically when the app starts.

For backend and frontend development without containers, install dependencies with `uv sync --frozen` and `npm ci --prefix frontend`. Run PostgreSQL with pgvector on port 55433 and set `DATABASE_URL` in `.env` to `postgresql+psycopg://skinder:skinder@127.0.0.1:55433/skinder`. Apply migrations using `uv run skinder-migrate`; start the API using `uv run skinder`; start the worker in another terminal with `uv run skinder-worker`. Run Vite with `npm --prefix frontend run dev` (http://127.0.0.1:5174). The production web entrypoint serves the built React files and API from the same origin.

## Configuration

`.env.example` lists all application settings and defaults. Copy it to `.env` for Compose or export equivalent variables in the service environment. Never put production credentials in the repository or in frontend variables such as `VITE_*`.

| Setting | Used by | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | All roles | PostgreSQL connection string. Compose overrides it to use its private `postgres` service. |
| `APP_ORIGIN` | Web | Public app origin, with no path. Use HTTPS in production. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Web | GitHub OAuth application credentials. Callback is `{APP_ORIGIN}/api/auth/callback`. |
| `SESSION_SECRET` | Web | At least 32 characters; signs browser sessions and OAuth state. |
| `LETSUR_API_KEY` | Worker | Server-side key for model requests. |
| `LETSUR_BASE_URL`, `LETSUR_MODEL` | Worker | HTTPS model API endpoint and model name. |
| `SKINDER_EMBEDDING_MODEL_DIR` | All roles | Bundled E5-small model path; image default is `/opt/skinder/models/e5-small`. |

Other `SKINDER_*` variables tune database pools, session expiry, model request limits, job budgets and timeouts, source fetching, and catalog sync. The example file documents their default values. Quotas reset at 00:00 UTC. A web service needs database, GitHub, and session settings. A worker needs database and Letsur settings. Ingest and migrations need only the database URL.

## Deploy on Railway

Create three Railway services from this repository and select the matching config file in each service's settings: [`railway.web.json`](railway.web.json), [`railway.worker.json`](railway.worker.json), and [`railway.ingest.json`](railway.ingest.json). They all build the same Docker image. The web service runs migrations before deployment and checks `/health/ready`; the worker has no HTTP health port. The ingest service runs on the `0 */6 * * *` schedule. Ensure only one scheduled ingest service is active for a database.

Provision PostgreSQL 18 with the pgvector 0.8.7 extension, enable persistent storage, and use the database provider's backups. Set `DATABASE_URL` on all three services to the provider's private connection URL. Keep the database private. Verify that backups are enabled and perform a restore exercise before relying on the deployment for durable user data; the application image and service filesystem are not a database backup.

Set these variables on Railway:

- **Web:** `APP_ORIGIN` (the public HTTPS origin), `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and a unique `SESSION_SECRET` with at least 32 characters.
- **Worker:** `LETSUR_API_KEY`.
- **All roles:** `DATABASE_URL` using the private PostgreSQL connection URL. Set `SKINDER_ENV=production` and `APP_ORIGIN` on web.

Set the GitHub OAuth callback URL to `{APP_ORIGIN}/api/auth/callback`. Use the exact same public origin in the GitHub OAuth app and `APP_ORIGIN`; cookies are secure when the origin uses HTTPS. Do not expose the database publicly or share the session secret across unrelated applications.

The web and worker services each open a database connection pool, so size PostgreSQL for the combined pool limits. Keep one web service and one or more workers within the configured concurrency and database capacity. The ingest schedule is best-effort: upstream caps, changing search results, and provider access limits mean it does not guarantee a complete catalog. Listings do not establish compatibility, license, or revision; these remain unknown without source evidence. Recommendations send the task, preferences, shortlisted descriptions, source excerpts, and relevant feedback to the configured model provider. Review that data flow and provider terms before inviting users.

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
