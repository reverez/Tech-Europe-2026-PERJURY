"""Live Gemini smoke for survivor analysis (#12) on the canonical refund survivors.

Requires GOOGLE_API_KEY (loaded from .env) and the single PERJURY_MODEL setting. Mutants are
executed locally (deterministically identical to Modal for this fixture); only the sanitized
refund context, the mutation diff and its description are sent to Gemini.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from perjury.analysis import GeminiSurvivorAnalyzer, SelectionStatus, select_hardening_target
from perjury.canonical import CanonicalRefundPlanner
from perjury.execution import execute_mutation_plan
from perjury.planning import plan_mutations
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit("ERROR: GOOGLE_API_KEY is missing; add it to .env for the live smoke.")
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    report = execute_mutation_plan(spec, baseline, plan, LocalPytestExecutor(), max_concurrency=4)
    selection = select_hardening_target(spec, baseline, plan, report, GeminiSurvivorAnalyzer())

    print(f"model={os.getenv('PERJURY_MODEL', 'google:gemini-3.8-flash')}")
    print(f"candidates={list(selection.candidate_ids)} status={selection.status.value}")
    for analysis in selection.analyses:
        print(analysis.model_dump_json(indent=2))
    print(selection.message)
    if selection.status is not SelectionStatus.SELECTED:
        raise SystemExit("FAIL: no hardening target selected from the canonical survivors")
    print(f"PASS: selected {selection.target.mutation_id} - {selection.target.summary}")


if __name__ == "__main__":
    main()
