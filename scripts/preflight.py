"""Explicit live-demo preflight. Exit code 0 only when every check passes.

    python scripts/preflight.py

Checks (grouped by subsystem): config, Gemini structured response, Modal auth + canary Sandbox,
baseline PASS (local + Modal parity), mutation-applicator self-check, evidence directory.
The full mutation rehearsal is separate: `python scripts/closed_loop_smoke.py`.
"""

from __future__ import annotations

import sys

from perjury.preflight import main

if __name__ == "__main__":
    sys.exit(main())
