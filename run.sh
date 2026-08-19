#!/usr/bin/env bash
# One-command run. Creates the venv, installs deps, starts the server.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  echo "→ creating .venv"
  "$PYTHON" -m venv .venv
fi

echo "→ installing dependencies"
./.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

echo "→ http://127.0.0.1:${PORT}"
exec ./.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port "$PORT"
