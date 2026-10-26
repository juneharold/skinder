#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
exec uv run skinder
