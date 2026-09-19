"""Live Gemini smoke for mutation planning (#10).

Requires GOOGLE_API_KEY (loaded from .env) and the single PERJURY_MODEL setting. Sends ONLY the
sanitized refund fixture context (implementation + test source) to Gemini.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from perjury.planning import GeminiMutationPlanner, PlanningConfig, plan_mutations
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit("ERROR: GOOGLE_API_KEY is missing; add it to .env for the live smoke.")
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, GeminiMutationPlanner(), PlanningConfig.from_env())
    print(
        f"model={os.getenv('PERJURY_MODEL', 'google:gemini-3.8-flash')} "
        f"accepted={len(plan.batch.mutations)} rejected={len(plan.rejected)} "
        f"replenishments={plan.replenishments_used}"
    )
    for mutation, applied in zip(plan.batch.mutations, plan.applied, strict=True):
        print(f"{mutation.id}: {mutation.description}\n{applied.diff}")
    for rejected in plan.rejected:
        print(f"rejected {rejected.proposed_id}: {rejected.reason.value} - {rejected.detail}")
    print("PASS: live Gemini plan validated through the deterministic gate")


if __name__ == "__main__":
    main()
