"""Modal concurrency gate (#7): 10 real refund mutants through bounded fan-out.

Runs the sanitized refund snapshot with ten isolated mutants, prints per-mutation outcomes,
and exits non-zero unless the gate holds. Requires `modal setup`. Uploads only the sanitized
WorkspaceSpec manifest (never .env/.git/.venv/.perjury).

    python scripts/modal_spike.py [--repeats N]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import modal

from perjury.contracts import ExecutionOutcome, MutationProposal
from perjury.fanout import FanoutJob, run_fanout
from perjury.modal_runner import ModalWorkspaceExecutor, _get_app
from perjury.mutation import apply_mutation
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]
FILE = "examples/refund/refund.py"
PREMIUM = "    if days_since_purchase <= 30 or premium:"

# (original, mutated, expected outcome), verified against the real fixture suite.
MUTATIONS = [
    # Boundary mutants survive: the fixture never tests day 30 (a genuine test gap).
    (PREMIUM, "    if days_since_purchase <= 29 or premium:", ExecutionOutcome.PASS),
    (PREMIUM, "    if days_since_purchase < 30 or premium:", ExecutionOutcome.PASS),
    (PREMIUM, "    if days_since_purchase <= 30 and premium:", ExecutionOutcome.TEST_FAIL),
    (PREMIUM, "    if days_since_purchase <= 30:", ExecutionOutcome.PASS),
    ("        return price\n", "        return price / 2\n", ExecutionOutcome.TEST_FAIL),
    ("    return 0.0", "    return price", ExecutionOutcome.TEST_FAIL),
    ("    if price < 0:", "    if price < -1:", ExecutionOutcome.PASS),
    ("    if days_since_purchase < 0:", "    if days_since_purchase < -1:", ExecutionOutcome.PASS),
    ("    return 0.0", "    return 1.0", ExecutionOutcome.TEST_FAIL),
    ("        return price\n", "        return price + 1\n", ExecutionOutcome.TEST_FAIL),
]


def running_sandboxes() -> int:
    """Count Sandboxes still alive in the PERJURY Modal app (leak check)."""
    return sum(1 for _ in modal.Sandbox.list(app_id=_get_app().app_id))


def run_once(index: int) -> bool:
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    workspaces = []
    try:
        for i, (original, mutated, _) in enumerate(MUTATIONS, start=1):
            proposal = MutationProposal(
                id=f"M{i:02d}",
                file_path=FILE,
                description=f"spike mutation {i}",
                hypothesis="concurrency spike",
                original_snippet=original,
                mutated_snippet=mutated,
            )
            workspaces.append(apply_mutation(spec, baseline, proposal))
        jobs = [FanoutJob(w.evidence.mutation_id, w.spec, w.manifest) for w in workspaces]
        result = run_fanout(jobs, ModalWorkspaceExecutor(), max_concurrency=10)
    finally:
        for workspace in workspaces:
            workspace.cleanup()

    print(f"-- run {index}: wall_clock={result.wall_clock_ms}ms peak={result.peak_concurrency}")
    problems: list[str] = []
    for item, (_, _, expected) in zip(result.results, MUTATIONS, strict=True):
        outcome = item.execution.outcome
        print(
            f"{item.mutation_id}: {outcome.value} status={item.status.value} "
            f"duration={item.duration_ms}ms cleanup_error={item.execution.cleanup_error}"
        )
        if outcome is not expected:
            problems.append(f"{item.mutation_id}: expected {expected.value}, got {outcome.value}")
        if item.execution.cleanup_error:
            problems.append(f"{item.mutation_id}: sandbox cleanup failed")
    if len(result.results) != len(MUTATIONS):
        problems.append("result count does not match requested mutations")
    if result.peak_concurrency < 2:
        problems.append("no concurrency observed")
    leaked = running_sandboxes()
    print(f"  running sandboxes after run: {leaked}")
    if leaked:
        problems.append(f"{leaked} Sandbox(es) still running after the run")
    for problem in problems:
        print(f"  GATE PROBLEM: {problem}")
    return not problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    passed = [run_once(i) for i in range(1, args.repeats + 1)]
    if not all(passed):
        raise SystemExit("FAIL: Modal fan-out gate")
    print(f"PASS: {len(passed)} fan-out run(s), 10 mutants each, one terminal result per mutant")


if __name__ == "__main__":
    main()
