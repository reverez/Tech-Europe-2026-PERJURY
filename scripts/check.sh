#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== PERJURY deterministic checks =="
python -m ruff check .
python -m compileall -q perjury scripts examples tests
python -c "from perjury.api import app; assert app.title == 'PERJURY'"
python -m pytest -q
