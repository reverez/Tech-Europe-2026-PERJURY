"""Deterministic closed-loop proof on the refund fixture (#16).

Model outputs are mocked, but the run goes through the same ``run_perjury`` path the application
uses, with real pytest executing every world locally. No Gemini/Modal is used; the live smokes
live in ``scripts/`` and are not part of CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from itertools import groupby
from pathlib import Path

import pytest
from loop_fakes import PREMIUM_TEST, FakeAnalyzer, FakeGenerator

from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import MutationStatus as M
from perjury.orchestrator import RunResult, RunStage, TerminalReason, run_perjury
from perjury.workspace import LocalPytestExecutor, build_snapshot_manifest, refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = refund_workspace_spec(str(ROOT))

# The stage/event contract. Changing the loop's stages or event order must fail this test.
EXPECTED_EVENT_SEQUENCE = [
    "run.started",
    "baseline.completed",
    "context.completed",
    "plan.completed",
    "mutation.completed",  # x8
    "execution.completed",
    "analysis.completed",
    "survivor.selected",
    "test.proposed",
    "candidate.ready",
    "verification.completed",
    "rescore.mutation.completed",  # x8
    "rescore.completed",
    "run.completed",
]


def go(run_id: str = "e2e") -> RunResult:
    return run_perjury(
        SPEC,
        planner=CanonicalRefundPlanner(),
        analyzer=FakeAnalyzer(),
        generator=FakeGenerator(),
        executor=LocalPytestExecutor(),
        run_id=run_id,
    )


@pytest.fixture(scope="module")
def loop() -> tuple[RunResult, str, bytes]:
    manifest_before = build_snapshot_manifest(SPEC).manifest_sha256
    existing = (ROOT / "examples/refund/test_refund.py").read_bytes()
    result = go()
    return result, manifest_before, existing


def test_baseline_passes(loop) -> None:
    result, *_ = loop
    assert result.baseline_outcome == "pass"
    assert result.stages[0].stage is RunStage.BASELINE and result.stages[0].evidence


def test_known_semantic_mutant_survives_the_initial_suite(loop) -> None:
    result, *_ = loop
    trials = {t.mutation_id: t for t in result.first_pass.trials}
    m01 = trials["M01"]
    assert m01.status is M.SURVIVED and m01.execution.outcome is O.PASS
    assert "-    if days_since_purchase <= 30 or premium:" in m01.diff
    assert "+    if days_since_purchase <= 30:" in m01.diff
    assert result.selected_mutation_id == "M01"
    assert result.selection.target.label == "potential test gap"
    assert (result.first_pass.score.killed, result.first_pass.score.survived) == (5, 3)


def test_candidate_is_a_separate_isolated_test_file(loop) -> None:
    result, manifest_before, existing = loop
    path = result.candidate.plan.candidate_path
    assert path == "examples/refund/test_perjury_M01.py"
    assert path != "examples/refund/test_refund.py"
    assert result.candidate.plan.code == PREMIUM_TEST
    assert "--- /dev/null" in result.candidate.plan.diff
    assert result.candidate.collection.outcome is O.PASS  # collected explicitly
    # the repository and the existing test file are untouched; the candidate never leaked in
    assert not (ROOT / path).exists()
    assert (ROOT / "examples/refund/test_refund.py").read_bytes() == existing
    assert build_snapshot_manifest(SPEC).manifest_sha256 == manifest_before


def test_candidate_passes_original_and_fails_the_selected_mutant(loop) -> None:
    result, *_ = loop
    v = result.verification
    assert v.original.outcome is O.PASS and v.original.exit_code == 0
    assert v.mutant.outcome is O.TEST_FAIL and v.mutant.exit_code == 1
    evidence = v.result.evidence
    assert evidence.verified and v.result.verdict == "verified"
    assert evidence.candidate_sha256 == result.candidate.plan.sha256
    assert evidence.base_manifest_sha256 == result.baseline_manifest_sha256
    assert evidence.pytest_argv[-1] == "examples/refund/test_perjury_M01.py"


def test_final_result_is_verified(loop) -> None:
    result, *_ = loop
    assert result.state is RunStage.VERIFIED and result.reason is TerminalReason.VERIFIED
    assert result.events[-1].type.value == "run.completed"
    assert result.events[-1].data["result"]["verdict"] == "verified"


def test_same_batch_rescore_has_defensible_before_after_evidence(loop) -> None:
    result, *_ = loop
    r = result.rescore
    c = r.comparison
    assert r.status.value == "confirmed" and result.improvement_verified
    assert c.batch_sha256 == result.batch_sha256  # same validated batch, not a substitute
    assert c.mutation_ids == tuple(m.id for m in result.plan.batch.mutations)
    assert [t.diff for t in r.after_report.trials] == [t.diff for t in result.first_pass.trials]
    assert (c.before.killed, c.before.survived, c.before.excluded_total) == (5, 3, 0)
    assert (c.after.killed, c.after.survived, c.after.excluded_total) == (6, 2, 0)
    assert c.before.score == 5 / 8 and c.after.score == 6 / 8
    assert c.delta == c.after.score - c.before.score == 0.125 and c.direction == "improved"
    assert c.newly_killed_ids == ("M01",) and c.newly_survived_ids == ()
    assert r.selected_before is M.SURVIVED and r.selected_after is M.KILLED
    survivors_after = {t.mutation_id for t in r.after_report.survivors()}
    assert survivors_after == {"M02", "M07"}  # the remaining, honestly reported test gaps


def test_stage_and_event_contract(loop) -> None:
    result, *_ = loop
    # consecutive per-mutation events collapse to one entry each
    dedup = [kind for kind, _ in groupby(e.type.value for e in result.events)]
    assert dedup == EXPECTED_EVENT_SEQUENCE
    assert [s.stage.value for s in result.stages] == [
        "baseline",
        "context",
        "planning",
        "mutation_execution",
        "survivor_analysis",
        "test_generation",
        "verification",
        "rescoring",
    ]


def test_the_run_is_reproducible_apart_from_timings(loop) -> None:
    first, *_ = loop
    second = go()

    def core(r: RunResult):
        return (
            r.state,
            r.reason,
            r.batch_sha256,
            r.context_sha256,
            r.rescore.comparison.model_dump(),
            r.candidate.plan.sha256,
            [
                (e.type, e.stage, e.mutation_id)
                for e in r.events
                if "mutation.completed" not in e.type
            ],
        )

    assert core(first) == core(second)


def test_deterministic_loop_needs_no_credentials_and_never_builds_the_live_agent() -> None:
    code = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, "tests")
        from test_e2e import go
        result = go("subprocess")
        assert result.state.value == "verified", result.state
        assert "perjury.agent" not in sys.modules
        print("ok")
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in {"GOOGLE_API_KEY", "GEMINI_API_KEY"}}
    env["PYTHONPATH"] = str(ROOT)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0 and done.stdout.strip().endswith("ok"), done.stdout + done.stderr


def test_breaking_the_candidate_breaks_the_verified_outcome() -> None:
    """Regression guard: a candidate that does not distinguish original from mutant cannot verify."""
    weak = run_perjury(
        SPEC,
        planner=CanonicalRefundPlanner(),
        analyzer=FakeAnalyzer(),
        generator=FakeGenerator(
            "from examples.refund.refund import calculate_refund\n\n\n"
            "def test_inside() -> None:\n    assert calculate_refund(5.0, 1, premium=False) == 5.0\n"
        ),
        executor=LocalPytestExecutor(),
        run_id="weak",
    )
    assert weak.state is RunStage.INCONCLUSIVE
    assert weak.reason is TerminalReason.MUTANT_NOT_KILLED and weak.rescore is None
