from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from perjury.canonical import CanonicalRefundPlanner, canonical_refund_batch
from perjury.contracts import (
    ExecutionOutcome,
    ExecutionResult,
    MutationStatus,
    WorkspaceSpec,
)
from perjury.execution import (
    MutationExecutionError,
    MutationExecutionReport,
    MutationScore,
    compute_mutation_score,
    execute_mutation_plan,
)
from perjury.fanout import FanoutJobResult, FanoutResult, run_fanout
from perjury.planning import MutationPlan, plan_mutations
from perjury.workspace import (
    BaselineResult,
    LocalPytestExecutor,
    refund_workspace_spec,
    run_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
K, S = MutationStatus.KILLED, MutationStatus.SURVIVED
I, T, E = MutationStatus.INVALID, MutationStatus.TIMEOUT, MutationStatus.INFRA_ERROR


def result(eid: str, outcome: ExecutionOutcome) -> ExecutionResult:
    code = {ExecutionOutcome.PASS: 0, ExecutionOutcome.TEST_FAIL: 1}.get(outcome)
    return ExecutionResult(
        execution_id=eid,
        outcome=outcome,
        exit_code=code,
        duration_ms=7,
        stdout=f"stdout {eid}",
        failure_detail=None if code is not None else f"{outcome.value} detail",
    )


class ScriptedExecutor:
    """Deterministic WorkspaceExecutor keyed by mutation id."""

    def __init__(self, outcomes: dict[str, ExecutionOutcome]) -> None:
        self.outcomes = outcomes
        self.seen: list[str] = []
        self.roots: list[str] = []
        self.lock = threading.Lock()

    def execute(self, spec, *, execution_id, manifest=None):
        with self.lock:
            self.seen.append(execution_id)
            self.roots.append(spec.source_root)
        return result(execution_id, self.outcomes[execution_id.removeprefix("mutation:")])


@pytest.fixture(scope="module")
def refund() -> tuple[WorkspaceSpec, BaselineResult, MutationPlan]:
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    return spec, baseline, plan_mutations(spec, baseline, CanonicalRefundPlanner())


def all_ids(plan: MutationPlan) -> list[str]:
    return [m.id for m in plan.batch.mutations]


def test_mixed_terminal_outcomes_are_projected_and_scored(refund) -> None:
    spec, baseline, plan = refund
    P, F = ExecutionOutcome.PASS, ExecutionOutcome.TEST_FAIL
    outcomes = dict(
        zip(
            all_ids(plan),
            [
                F,
                F,
                P,
                ExecutionOutcome.INVALID,
                ExecutionOutcome.TIMEOUT,
                ExecutionOutcome.INFRA_ERROR,
                F,
                P,
            ],
            strict=True,
        )
    )
    report = execute_mutation_plan(spec, baseline, plan, ScriptedExecutor(outcomes))
    assert [t.status for t in report.trials] == [K, K, S, I, T, E, K, S]
    s = report.score
    assert (s.killed, s.survived, s.invalid, s.timeout, s.infra_error) == (3, 2, 1, 1, 1)
    assert s.valid_total == 5 and s.excluded_total == 3
    assert s.state == "scored" and s.score == 3 / 5
    invalid = next(t for t in report.trials if t.status is I)
    assert invalid.execution.failure_detail == "invalid detail"  # excluded but visible
    assert {t.mutation_id for t in report.survivors()} == {"M03", "M08"}


def test_exactly_one_trial_per_accepted_id_in_plan_order_with_evidence(refund) -> None:
    spec, baseline, plan = refund
    executor = ScriptedExecutor({i: ExecutionOutcome.TEST_FAIL for i in all_ids(plan)})
    report = execute_mutation_plan(spec, baseline, plan, executor, max_concurrency=3)
    assert [t.mutation_id for t in report.trials] == all_ids(plan)
    assert sorted(executor.seen) == sorted(f"mutation:{i}" for i in all_ids(plan))
    for trial, applied in zip(report.trials, plan.applied, strict=True):
        assert trial.diff == applied.diff and trial.target_path == applied.target_path
        assert trial.execution.stdout == f"stdout mutation:{trial.mutation_id}"
    assert report.max_concurrency == 3 and report.wall_clock_ms >= 0
    assert report.base_manifest_sha256 == baseline.manifest_sha256
    assert report.context_sha256 == plan.context_sha256


def test_each_mutation_runs_in_its_own_isolated_workspace_and_is_cleaned_up(refund) -> None:
    spec, baseline, plan = refund
    executor = ScriptedExecutor({i: ExecutionOutcome.PASS for i in all_ids(plan)})
    execute_mutation_plan(spec, baseline, plan, executor)
    assert len(set(executor.roots)) == len(all_ids(plan))
    assert spec.source_root not in executor.roots
    assert not any(Path(r).exists() for r in executor.roots)


def test_workspace_cleanup_happens_even_when_fanout_raises(refund) -> None:
    spec, baseline, plan = refund
    roots: list[str] = []

    def exploding(jobs, executor, **_kw):
        roots.extend(j.spec.source_root for j in jobs)
        raise RuntimeError("fatal")

    with pytest.raises(RuntimeError, match="fatal"):
        execute_mutation_plan(spec, baseline, plan, ScriptedExecutor({}), fanout_runner=exploding)
    assert roots and not any(Path(r).exists() for r in roots)


@pytest.mark.parametrize("mode", ["missing", "extra", "duplicate"])
def test_cardinality_mismatch_is_rejected(refund, mode: str) -> None:
    spec, baseline, plan = refund

    def broken(jobs, executor, **kw):
        good = run_fanout(jobs, executor, **kw)
        items = list(good.results)
        if mode == "missing":
            items = items[:-1]
        elif mode == "extra":
            ghost = items[0].model_copy(update={"mutation_id": "M99"})
            items.append(ghost)
        else:
            items.append(items[0])
        return FanoutResult(**{**good.model_dump(), "results": tuple(items)})

    executor = ScriptedExecutor({i: ExecutionOutcome.TEST_FAIL for i in all_ids(plan)})
    with pytest.raises(MutationExecutionError):
        execute_mutation_plan(spec, baseline, plan, executor, fanout_runner=broken)


def test_all_infrastructure_errors_yield_inconclusive_not_zero(refund) -> None:
    spec, baseline, plan = refund
    executor = ScriptedExecutor({i: ExecutionOutcome.INFRA_ERROR for i in all_ids(plan)})
    report = execute_mutation_plan(spec, baseline, plan, executor)
    assert report.score.valid_total == 0
    assert report.score.state == "inconclusive" and report.score.score is None
    assert report.score.infra_error == len(all_ids(plan))
    assert len(report.trials) == len(all_ids(plan))  # evidence retained


def test_score_uses_only_killed_and_survived() -> None:
    assert compute_mutation_score([K, K, K, S]).score == 0.75
    assert compute_mutation_score([K, S, I, I, T, E, E]).score == 0.5
    only_killed = compute_mutation_score([K, I])
    assert only_killed.score == 1.0 and only_killed.excluded_total == 1
    only_survived = compute_mutation_score([S])
    assert only_survived.score == 0.0 and only_survived.state == "scored"


def test_zero_denominator_is_explicit() -> None:
    for statuses in ([], [I], [T, E, I]):
        score = compute_mutation_score(statuses)
        assert score.state == "inconclusive" and score.score is None and score.valid_total == 0


def test_score_contract_rejects_inconsistent_values() -> None:
    good = compute_mutation_score([K, S])
    with pytest.raises(ValidationError):
        MutationScore(**{**good.model_dump(), "score": 0.9})
    with pytest.raises(ValidationError):
        MutationScore(**{**compute_mutation_score([I]).model_dump(), "score": 0.0})
    with pytest.raises(ValidationError):
        MutationScore(**{**good.model_dump(), "valid_total": 5})


def test_report_rejects_tampered_score_and_duplicate_ids(refund) -> None:
    spec, baseline, plan = refund
    executor = ScriptedExecutor({i: ExecutionOutcome.TEST_FAIL for i in all_ids(plan)})
    report = execute_mutation_plan(spec, baseline, plan, executor)
    data = report.model_dump()
    data["score"] = compute_mutation_score([S]).model_dump()
    with pytest.raises(ValidationError, match="Score does not match"):
        MutationExecutionReport.model_validate(data)
    data = report.model_dump()
    data["trials"] = [data["trials"][0], data["trials"][0]]
    with pytest.raises(ValidationError):
        MutationExecutionReport.model_validate(data)
    assert MutationExecutionReport.model_validate_json(report.model_dump_json()) == report


def test_progress_callback_receives_each_terminal_result(refund) -> None:
    spec, baseline, plan = refund
    seen: list[FanoutJobResult] = []
    executor = ScriptedExecutor({i: ExecutionOutcome.PASS for i in all_ids(plan)})
    execute_mutation_plan(spec, baseline, plan, executor, on_result=seen.append)
    assert sorted(r.mutation_id for r in seen) == sorted(all_ids(plan))


def test_canonical_refund_batch_has_the_meaningful_premium_survivor(refund) -> None:
    """Real pytest against every canonical mutant (no Modal): the premium mutant must survive."""
    spec, baseline, plan = refund
    assert len(canonical_refund_batch().mutations) == 8
    report = execute_mutation_plan(spec, baseline, plan, LocalPytestExecutor(), max_concurrency=4)
    by_id = {t.mutation_id: t for t in report.trials}
    premium = by_id["M01"]
    assert premium.status is S
    assert "-    if days_since_purchase <= 30 or premium:" in premium.diff
    assert "+    if days_since_purchase <= 30:" in premium.diff
    assert report.score.killed >= 1 and report.score.survived >= 1
    assert report.score.excluded_total == 0
    assert report.score.score == report.score.killed / report.score.valid_total
