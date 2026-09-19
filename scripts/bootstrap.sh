#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== PERJURY bootstrap =="
echo "repo: $ROOT"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: missing required command: $1" >&2
    exit 1
  fi
}

need python3
need git

PYTHON_VERSION="$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"

python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("ERROR: Python 3.12+ is required.")
print(f"Python OK: {sys.version.split()[0]}")
PY

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Upgrading packaging tools..."
python -m pip install --upgrade pip setuptools wheel

echo "Installing PERJURY + dev dependencies..."
python -m pip install -e ".[dev]"

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo ".env already exists; leaving it untouched"
fi

echo
echo "Authenticating Modal..."
if ! command -v modal >/dev/null 2>&1; then
  echo "ERROR: modal CLI missing after install" >&2
  exit 1
fi
modal setup

echo
echo "Installing Modal agent skills..."
modal skills install --global || {
  echo "WARNING: Modal skills install failed; continuing because it is not required for runtime."
}

echo
echo "Running local test suite..."
pytest -q

echo
echo "Running Modal concurrency spike..."
python scripts/modal_spike.py

echo
echo "Bootstrap complete."
echo "Next:"
echo "  1) Put GOOGLE_API_KEY in .env"
echo "  2) Run: python scripts/gemini_smoke.py"
echo "  3) Then continue with issue #2 (end-to-end hardening loop)"
