#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== PERJURY deterministic checks =="
python -m pip check
python -m ruff check .
python -m compileall -q perjury scripts examples tests
python -c "from perjury.api import app; assert app.title == 'PERJURY'"
python -c "from perjury.modal_runner import PYTEST_VERSION; assert PYTEST_VERSION == '9.1.1'"
python -m pytest -q
