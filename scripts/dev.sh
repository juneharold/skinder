#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
if [ ! -d frontend/node_modules ]; then
  npm --prefix frontend ci
fi
uv run uvicorn skinder.app:create_app --factory --host 127.0.0.1 --port 8742 --reload &
backend_pid=$!
npm --prefix frontend run dev &
frontend_pid=$!
cleanup() {
  kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
  wait "$backend_pid" "$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
printf 'Skinder development: http://127.0.0.1:5174\n'
wait
