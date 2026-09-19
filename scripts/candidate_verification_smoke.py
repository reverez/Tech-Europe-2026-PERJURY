"""Live gate for #13/#14: canonical M01 premium candidate verified through Modal.

Uses a fixed candidate test (no Gemini call) and the sanitized snapshot only. Runs the collection
preflight and both verification worlds in Modal Sandboxes through the shared executor.
"""

from __future__ import annotations

from pathlib import Path

import modal

from perjury.candidate import plan_candidate, preflight_candidate
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import TestProposal
from perjury.hardening import verify_candidate
from perjury.modal_runner import ModalWorkspaceExecutor, _get_app
from perjury.planning import plan_mutations
from perjury.workspace import (
    LocalPytestExecutor,
    build_snapshot_manifest,
    refund_workspace_spec,
    run_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_premium_customer_outside_window_still_gets_refund() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
)


def main() -> None:
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    m01 = next(m for m in plan.batch.mutations if m.id == "M01")
    executor = ModalWorkspaceExecutor()

    candidate = plan_candidate(
        spec,
        baseline,
        TestProposal(
            mutation_id="M01",
            test_name="test_premium_customer_outside_window_still_gets_refund",
            target_file="examples/refund/test_refund.py",
            test_code=CANDIDATE,
            explanation="premium customers outside the 30-day window keep refund eligibility",
        ),
    )
    evidence = preflight_candidate(spec, baseline, candidate, executor)
    print(f"preflight: {evidence.collection.outcome.value} path={candidate.candidate_path}")

    result = verify_candidate(spec, baseline, m01, candidate, executor)
    e = result.result.evidence
    print(
        f"original: {result.original.outcome.value} exit={e.original_exit_code} "
        f"{e.original_duration_ms}ms cleanup={result.original.cleanup_error}"
    )
    print(
        f"mutant:   {result.mutant.outcome.value} exit={e.mutant_exit_code} "
        f"{e.mutant_duration_ms}ms cleanup={result.mutant.cleanup_error}"
    )
    print(f"verdict={result.result.verdict} candidate_sha256={e.candidate_sha256}")

    leaked = sum(1 for _ in modal.Sandbox.list(app_id=_get_app().app_id))
    problems = []
    if result.result.verdict != "verified":
        problems.append(f"verdict is {result.result.verdict}")
    if leaked:
        problems.append(f"{leaked} Sandbox(es) still running")
    if (ROOT / candidate.candidate_path).exists():
        problems.append("candidate file leaked into the repository")
    if build_snapshot_manifest(spec).manifest_sha256 != baseline.manifest_sha256:
        problems.append("repository changed during verification")
    for problem in problems:
        print(f"GATE PROBLEM: {problem}")
    if problems:
        raise SystemExit("FAIL: canonical M01 verification gate")
    print("PASS: M01 candidate verified (original PASS, mutant TEST_FAIL), repo untouched")


if __name__ == "__main__":
    main()
