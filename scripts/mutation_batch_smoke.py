"""Live gate for #11: the canonical refund batch through bounded Modal fan-out.

Uploads only the sanitized WorkspaceSpec snapshot (never .env/.git/.venv/.perjury). Uses the
deterministic canonical batch, so no Gemini call is made.
"""

from __future__ import annotations

from pathlib import Path

import modal

from perjury.canonical import CanonicalRefundPlanner
from perjury.execution import execute_mutation_plan
from perjury.modal_runner import ModalWorkspaceExecutor, _get_app
from perjury.planning import plan_mutations
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    report = execute_mutation_plan(spec, baseline, plan, ModalWorkspaceExecutor())

    print(f"wall_clock={report.wall_clock_ms}ms peak_concurrency={report.peak_concurrency}")
    for trial in report.trials:
        print(
            f"{trial.mutation_id}: {trial.status.value} ({trial.execution.outcome.value}) "
            f"{trial.duration_ms}ms cleanup_error={trial.execution.cleanup_error}"
        )
    print(report.score.model_dump_json())

    leaked = sum(1 for _ in modal.Sandbox.list(app_id=_get_app().app_id))
    problems: list[str] = []
    if [t.mutation_id for t in report.trials] != [m.id for m in plan.batch.mutations]:
        problems.append("trial IDs do not match accepted IDs")
    if report.score.state != "scored":
        problems.append("score is inconclusive")
    if report.score.excluded_total:
        problems.append(f"{report.score.excluded_total} excluded (invalid/timeout/infra) trial(s)")
    survivors = {t.mutation_id for t in report.survivors()}
    if "M01" not in survivors:
        problems.append("premium demo mutant M01 did not survive")
    if leaked:
        problems.append(f"{leaked} Sandbox(es) still running")
    if any(t.execution.cleanup_error for t in report.trials):
        problems.append("Sandbox cleanup error")
    for problem in problems:
        print(f"GATE PROBLEM: {problem}")
    if problems:
        raise SystemExit("FAIL: canonical refund batch Modal gate")
    print(f"PASS: 8 mutants, score={report.score.score:.3f}, survivors={sorted(survivors)}")


if __name__ == "__main__":
    main()
