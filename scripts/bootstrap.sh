#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== PERJURY deterministic bootstrap =="
echo "repo: $ROOT"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: missing required command: $1" >&2
    exit 1
  fi
}

need python3
need git

python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"ERROR: Python 3.12.x is required; found {sys.version.split()[0]}."
    )
print(f"Python 3.12 runtime OK: {sys.version.split()[0]}")
PY

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing pinned PERJURY + dev dependencies..."
python -m pip install -e ".[dev]"

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo ".env already exists; leaving it untouched"
fi

echo
echo "Running deterministic quality gate..."
bash scripts/check.sh

echo
echo "Bootstrap complete."
echo "External services are deliberately NOT invoked by bootstrap."
echo "Next:"
echo "  1) Continue M0 with issue #6 (sanitized Modal workspace materialization)."
echo "  2) Put GOOGLE_API_KEY in .env, then run: python scripts/gemini_smoke.py"
echo "  3) Authenticate Modal separately with: modal setup"
echo "  4) Run the Modal smoke separately with: python scripts/modal_spike.py"
echo "  5) Follow parent epic #1 and docs/IMPLEMENTATION_PLAN.md"
