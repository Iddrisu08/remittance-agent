#!/usr/bin/env bash
# Run the test suite against a throwaway database.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
[ -d .venv ] || "$PYTHON" -m venv .venv
./.venv/bin/pip install -q --disable-pip-version-check -r requirements-dev.txt
exec ./.venv/bin/python -m pytest -q "$@"
