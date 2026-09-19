from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from perjury.candidate import plan_candidate
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import ExecutionResult, MutationStatus
from perjury.contracts import TestProposal as GeneratedTest
from perjury.execution import MutationExecutionReport, execute_mutation_plan
from perjury.hardening import verify_candidate
from perjury.planning import plan_mutations
from perjury.rescore import (
    RescoreError,
    RescoreStatus,
    ScoreComparison,
    batch_identity,
    compare_scores,
    rescore_with_candidate,
)
from perjury.workspace import (
    LocalPytestExecutor,
    build_snapshot_manifest,
    refund_workspace_spec,
    run_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
K, S, I, T, E = (
    MutationStatus.KILLED,
    MutationStatus.SURVIVED,
    MutationStatus.INVALID,
    MutationStatus.TIMEOUT,
    MutationStatus.INFRA_ERROR,
)
PREMIUM = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_premium_outside_window() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
)
WEAK = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_inside() -> None:\n    assert calculate_refund(5.0, 1, premium=False) == 5.0\n"
)
OUTCOME_FOR = {K: O.TEST_FAIL, S: O.PASS, I: O.INVALID, T: O.TIMEOUT, E: O.INFRA_ERROR}
FIRST_PASS = {"M01": S, "M02": S, "M03": K, "M04": K, "M05": K, "M06": K, "M07": S, "M08": K}


def result(eid: str, status: MutationStatus) -> ExecutionResult:
    outcome = OUTCOME_FOR[status]
    code = {O.PASS: 0, O.TEST_FAIL: 1, O.INVALID: 2}.get(outcome)
    return ExecutionResult(
        execution_id=eid, outcome=outcome, exit_code=code, duration_ms=2, failure_detail="x"
    )


class TwoPassExecutor:
    """Scripted: different outcomes when the candidate is present in the workspace."""

    def __init__(self, before: dict, after: dict) -> None:
        self.before, self.after = before, after
        self.after_calls: list[tuple] = []
        self.lock = threading.Lock()

    def execute(self, spec, *, execution_id, manifest=None):
        mid = execution_id.removeprefix("mutation:")
        candidate = next((a for a in spec.pytest_argv if "test_perjury" in a), None)
        if candidate is None:
            return result(execution_id, self.before[mid])
        root = Path(spec.source_root)
        with self.lock:
            self.after_calls.append(
                (mid, spec.pytest_argv, (root / candidate).read_bytes(), spec.snapshot_id)
            )
        return result(execution_id, self.after[mid])


@pytest.fixture(scope="module")
def world():
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    m01 = plan.batch.mutations[0]

    def candidate(code):
        return plan_candidate(
            spec,
            baseline,
            GeneratedTest(
                mutation_id="M01",
                test_name="t",
                target_file="examples/refund/test_refund.py",
                test_code=code,
                explanation="e",
            ),
        )

    good, weak = candidate(PREMIUM), candidate(WEAK)
    verified = verify_candidate(spec, baseline, m01, good, LocalPytestExecutor())
    inconclusive = verify_candidate(spec, baseline, m01, weak, LocalPytestExecutor())
    assert verified.result.verdict == "verified" and inconclusive.result.verdict == "inconclusive"
    return spec, baseline, plan, good, verified, inconclusive


def first_pass(world, executor=None):
    spec, baseline, plan, *_ = world
    executor = executor or TwoPassExecutor(FIRST_PASS, {})
    return execute_mutation_plan(spec, baseline, plan, executor)


def rescore(world, after: dict, before_map=FIRST_PASS):
    spec, baseline, plan, cand, verified, _ = world
    executor = TwoPassExecutor(before_map, after)
    before = first_pass(world, executor)
    report = rescore_with_candidate(spec, baseline, plan, before, verified, cand, executor)
    return report, executor


def test_real_pytest_improvement_uses_the_same_batch_and_kills_the_selected_mutant(world) -> None:
    spec, baseline, plan, cand, verified, _ = world
    manifest_before = build_snapshot_manifest(spec).manifest_sha256
    before = execute_mutation_plan(spec, baseline, plan, LocalPytestExecutor(), max_concurrency=4)
    report = rescore_with_candidate(
        spec, baseline, plan, before, verified, cand, LocalPytestExecutor(), max_concurrency=4
    )
    c = report.comparison
    assert report.status is RescoreStatus.CONFIRMED and report.improvement_verified
    assert (c.before.killed, c.before.survived) == (5, 3)
    assert (c.after.killed, c.after.survived) == (6, 2)
    assert c.before.score == 0.625 and c.after.score == 0.75 and c.delta == 0.125
    assert c.direction == "improved" and c.killed_delta == 1
    assert c.newly_killed_ids == ("M01",) and c.newly_survived_ids == ()
    assert report.selected_before is S and report.selected_after is K
    assert c.mutation_ids == tuple(m.id for m in plan.batch.mutations)
    assert c.batch_sha256 == batch_identity(plan)
    assert [t.mutation_id for t in report.after_report.trials] == list(c.mutation_ids)
    assert [t.diff for t in report.after_report.trials] == [t.diff for t in before.trials]
    assert build_snapshot_manifest(spec).manifest_sha256 == manifest_before
    assert not (ROOT / cand.candidate_path).exists()
    assert MutationExecutionReport.model_validate_json(report.after_report.model_dump_json())
    assert type(report).model_validate_json(report.model_dump_json()) == report


def test_every_mutant_workspace_gets_the_byte_identical_candidate(world) -> None:
    *_, cand, _, _ = world
    report, executor = rescore(world, {**FIRST_PASS, "M01": K})
    assert report.status is RescoreStatus.CONFIRMED
    assert sorted(c[0] for c in executor.after_calls) == sorted(FIRST_PASS)
    for _mid, argv, code, _snapshot in executor.after_calls:
        assert argv[-1] == cand.candidate_path
        assert hashlib.sha256(code).hexdigest() == cand.sha256
    assert len({c[3] for c in executor.after_calls}) == len(FIRST_PASS)  # isolated worlds


def test_all_valid_mutants_are_rerun_including_previously_killed_ones(world) -> None:
    report, executor = rescore(world, {**FIRST_PASS, "M01": K})
    assert {c[0] for c in executor.after_calls} == set(FIRST_PASS)
    assert all(t.status is not None for t in report.after_report.trials)


def test_invalid_timeout_infra_are_excluded_and_retained_separately(world) -> None:
    after = {**FIRST_PASS, "M01": K, "M02": I, "M07": T, "M08": E}
    report, _ = rescore(world, after)
    a = report.comparison.after
    assert (a.killed, a.survived, a.invalid, a.timeout, a.infra_error) == (5, 0, 1, 1, 1)
    assert a.valid_total == 5 and a.excluded_total == 3 and a.score == 1.0
    assert report.comparison.before.score == 0.625
    # excluded trials keep their evidence
    invalid = next(t for t in report.after_report.trials if t.mutation_id == "M02")
    assert invalid.execution.outcome is O.INVALID and invalid.execution.failure_detail
    # M08 was killed before and is infra now: excluded, not "newly survived", still consistent
    assert report.status is RescoreStatus.CONFIRMED and report.comparison.newly_survived_ids == ()


def test_selected_mutant_not_killed_makes_the_replay_inconsistent(world) -> None:
    report, _ = rescore(world, dict(FIRST_PASS))  # nothing changes: M01 still survives
    assert report.status is RescoreStatus.INCONSISTENT and not report.improvement_verified
    assert report.comparison.direction == "unchanged" and report.comparison.delta == 0
    assert any("M01" in p and "not killed" in p for p in report.inconsistencies)
    assert "no improvement is claimed" in report.message


@pytest.mark.parametrize("bad", [I, T, E])
def test_selected_mutant_excluded_on_replay_is_inconsistent(world, bad) -> None:
    report, _ = rescore(world, {**FIRST_PASS, "M01": bad})
    assert report.status is RescoreStatus.INCONSISTENT and not report.improvement_verified


def test_previously_killed_mutant_that_survives_is_inconsistent(world) -> None:
    report, _ = rescore(world, {**FIRST_PASS, "M01": K, "M03": S})
    assert report.status is RescoreStatus.INCONSISTENT
    assert report.comparison.newly_survived_ids == ("M03",)
    assert not report.improvement_verified


def test_zero_valid_after_side_has_no_fabricated_score_or_delta(world) -> None:
    report, _ = rescore(world, {k: E for k in FIRST_PASS})
    c = report.comparison
    assert c.after.state == "inconclusive" and c.after.score is None
    assert c.delta is None and c.direction == "unavailable"
    assert report.status is RescoreStatus.INCONSISTENT  # selected mutant was not killed


def test_compare_scores_zero_valid_before_side_and_no_change(world) -> None:
    _, _, plan, *_ = world
    ids = [m.id for m in plan.batch.mutations]

    def report_of(statuses):
        base = first_pass(world)
        trials = [
            t.model_copy(update={"status": s, "execution": result(t.execution.execution_id, s)})
            for t, s in zip(base.trials, statuses, strict=True)
        ]
        from perjury.execution import compute_mutation_score

        return base.model_copy(
            update={"trials": tuple(trials), "score": compute_mutation_score(statuses)}
        )

    dead, same = report_of([I] * len(ids)), first_pass(world)
    c = compare_scores(plan, dead, same)
    assert c.before.score is None and c.delta is None and c.direction == "unavailable"
    unchanged = compare_scores(plan, same, same)
    assert unchanged.direction == "unchanged" and unchanged.delta == 0


def test_comparison_contract_rejects_fabricated_delta(world) -> None:
    report, _ = rescore(world, {**FIRST_PASS, "M01": K})
    data = report.comparison.model_dump()
    with pytest.raises(ValidationError):
        ScoreComparison.model_validate({**data, "delta": 0.9})
    with pytest.raises(ValidationError):
        ScoreComparison.model_validate({**data, "direction": "regressed"})
    dead = {k: E for k in FIRST_PASS}
    data = rescore(world, dead)[0].comparison.model_dump()
    with pytest.raises(ValidationError):
        ScoreComparison.model_validate({**data, "delta": 0.1, "direction": "improved"})


def test_rescore_requires_a_verified_candidate(world) -> None:
    spec, baseline, plan, cand, _, inconclusive = world
    executor = TwoPassExecutor(FIRST_PASS, {})
    before = first_pass(world, executor)
    with pytest.raises(RescoreError, match="VERIFIED"):
        rescore_with_candidate(spec, baseline, plan, before, inconclusive, cand, executor)
    assert executor.after_calls == []  # nothing was replayed


def test_rescore_rejects_mismatched_identities(world) -> None:
    spec, baseline, plan, cand, verified, _ = world
    executor = TwoPassExecutor(FIRST_PASS, {**FIRST_PASS, "M01": K})
    before = first_pass(world, executor)

    other = cand.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(RescoreError, match="does not describe"):
        rescore_with_candidate(spec, baseline, plan, before, verified, other, executor)

    foreign = MutationExecutionReport.model_validate(
        {**before.model_dump(), "context_sha256": "sha256:" + "2" * 64}
    )
    with pytest.raises(RescoreError, match="does not belong"):
        rescore_with_candidate(spec, baseline, plan, foreign, verified, cand, executor)

    short = before.model_copy(update={"trials": before.trials[:-1]})
    with pytest.raises(RescoreError):
        rescore_with_candidate(spec, baseline, plan, short, verified, cand, executor)


def test_batch_identity_changes_when_the_batch_changes(world) -> None:
    _, _, plan, *_ = world
    tampered = plan.model_copy(update={"applied": plan.applied[:-1]})
    assert batch_identity(plan) != batch_identity(tampered)
    assert batch_identity(plan) == batch_identity(plan)
