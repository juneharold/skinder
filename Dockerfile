# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim-bookworm AS python-build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN .venv/bin/python -c "from skinder.retrieval import prepare_model; prepare_model('/opt/skinder/models/e5-small')"

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SKINDER_EMBEDDING_MODEL_DIR=/opt/skinder/models/e5-small
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system skinder \
    && useradd --system --gid skinder --home-dir /app skinder
WORKDIR /app
COPY --from=python-build --chown=skinder:skinder /app/.venv /app/.venv
COPY --from=python-build --chown=skinder:skinder /app/src /app/src
COPY --from=python-build --chown=skinder:skinder /opt/skinder/models/e5-small /opt/skinder/models/e5-small
COPY --from=frontend --chown=skinder:skinder /build/frontend/dist /app/frontend/dist
USER skinder
EXPOSE 8742
CMD ["skinder"]
