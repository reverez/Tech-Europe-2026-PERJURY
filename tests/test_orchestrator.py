from __future__ import annotations

import json
from pathlib import Path

import pytest
from loop_fakes import (
    PREMIUM_TEST,
    WEAK_TEST,
    WRONG_TEST,
    FakeAnalyzer,
    FakeGenerator,
    ScriptedExecutor,
)

from perjury import orchestrator
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.orchestrator import (
    ALLOWED_NEXT,
    TERMINAL_STAGES,
    WORKING_STAGES,
    InvalidTransitionError,
    RunConfig,
    RunEventType,
    RunResult,
    RunStage,
    TerminalReason,
    _Run,
    run_perjury,
)
from perjury.workspace import refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = refund_workspace_spec(str(ROOT))
S, F = RunStage, TerminalReason


class BrokenPlanner:
    def propose(self, request):
        return {"rationale": "r", "mutations": [{"id": "bad"}]}


class ExplodingPlanner:
    def propose(self, request):
        raise RuntimeError("quota")


def run(executor=None, *, planner=None, analyzer=None, generator=None, **kw) -> RunResult:
    return run_perjury(
        SPEC,
        planner=planner or CanonicalRefundPlanner(),
        analyzer=analyzer or FakeAnalyzer(),
        generator=generator or FakeGenerator(),
        executor=executor or ScriptedExecutor(),
        run_id=kw.pop("run_id", "run-test"),
        **kw,
    )


def types(result: RunResult) -> list[str]:
    return [e.type.value for e in result.events]


def stages(result: RunResult) -> list[RunStage]:
    return [s.stage for s in result.stages]


# ---------------------------------------------------------------- state machine


def test_transitions_are_monotonic_and_terminal_states_are_final() -> None:
    assert WORKING_STAGES[0] is S.CREATED
    for i, stage in enumerate(WORKING_STAGES):
        allowed = ALLOWED_NEXT[stage]
        assert TERMINAL_STAGES <= allowed
        working_next = allowed - TERMINAL_STAGES
        assert working_next == ({WORKING_STAGES[i + 1]} if i + 1 < len(WORKING_STAGES) else set())
    assert all(ALLOWED_NEXT[t] == frozenset() for t in TERMINAL_STAGES)


@pytest.mark.parametrize(
    "path,bad",
    [
        ([], S.PLANNING),  # skipping stages
        ([S.BASELINE, S.CONTEXT], S.BASELINE),  # going backwards
        ([S.BASELINE], S.BASELINE),  # repeating
        ([S.VERIFIED], S.BASELINE),  # nothing after a terminal state
        ([S.FAILED], S.VERIFIED),
        ([S.BASELINE], S.CREATED),
    ],
)
def test_invalid_transitions_are_rejected(path, bad) -> None:
    machine = _Run("r", lambda: 0.0, None)
    for stage in path:
        machine.transition(stage)
    with pytest.raises(InvalidTransitionError):
        machine.transition(bad)
    assert machine.stage is (path[-1] if path else S.CREATED)  # state unchanged on rejection


def test_run_result_rejects_incoherent_state() -> None:
    good = run()
    data = good.model_dump()
    with pytest.raises(ValueError):
        RunResult.model_validate({**data, "state": "baseline"})  # non-terminal
    with pytest.raises(ValueError):
        RunResult.model_validate({**data, "events": data["events"][1:]})  # gap in seq
    with pytest.raises(ValueError):
        RunResult.model_validate({**data, "rescore": None})  # VERIFIED needs re-score


# ---------------------------------------------------------------- verified path


def test_verified_path_records_every_stage_with_timing_and_evidence_refs() -> None:
    result = run()
    assert result.state is S.VERIFIED and result.reason is F.VERIFIED
    assert stages(result) == [s for s in WORKING_STAGES if s is not S.CREATED]
    assert all(s.status == "ok" and s.duration_ms >= 0 for s in result.stages)
    assert all(s.evidence for s in result.stages)  # every stage carries evidence references
    by = {s.stage: s.evidence for s in result.stages}
    assert by[S.BASELINE]["manifest_sha256"] == result.baseline_manifest_sha256
    assert by[S.CONTEXT]["context_sha256"] == result.context_sha256
    assert by[S.PLANNING]["batch_sha256"] == result.batch_sha256
    assert by[S.RESCORING]["batch_sha256"] == result.batch_sha256
    assert result.selected_mutation_id == "M01"
    assert result.improvement_verified
    assert RunResult.model_validate_json(result.model_dump_json()) == result


def test_events_are_ordered_typed_correlated_and_end_with_the_terminal_state() -> None:
    result = run()
    assert [e.seq for e in result.events] == list(range(1, len(result.events) + 1))
    assert types(result)[0] == "run.started" and types(result)[-1] == "run.completed"
    assert all(e.run_id == "run-test" for e in result.events)
    assert result.events[-1].stage is S.VERIFIED
    rank = {s: i for i, s in enumerate(list(WORKING_STAGES) + [S.VERIFIED])}
    ranks = [rank[e.stage] for e in result.events]
    assert ranks == sorted(ranks)  # stages never go backwards in the event stream
    mutation_events = [e for e in result.events if e.type is RunEventType.MUTATION_COMPLETED]
    assert sorted(e.mutation_id for e in mutation_events) == [f"M0{i}" for i in range(1, 9)]
    assert all(
        e.correlation_id == f"run-test:mutation_execution:{e.mutation_id}" for e in mutation_events
    )
    rescore_events = [e for e in result.events if e.type is RunEventType.RESCORE_MUTATION_COMPLETED]
    assert len(rescore_events) == 8
    payload = next(e for e in result.events if e.type is RunEventType.RESCORE_COMPLETED).data
    assert payload["before"]["score"] == 5 / 8 and payload["after"]["score"] == 6 / 8
    assert payload["delta"] == 6 / 8 - 5 / 8 and payload["direction"] == "improved"
    json.dumps([e.model_dump(mode="json") for e in result.events])  # serializable


def test_fake_clock_gives_deterministic_monotonic_timings() -> None:
    ticks = iter(range(10_000))
    result = run(clock=lambda: next(ticks) / 1000)
    assert [e.at_ms for e in result.events] == sorted(e.at_ms for e in result.events)
    for stage in result.stages:
        assert stage.ended_ms - stage.started_ms == stage.duration_ms
    assert result.total_ms >= result.events[-1].at_ms


def test_observer_receives_events_and_its_failures_never_break_the_run() -> None:
    seen: list[str] = []
    ok = run(on_event=lambda e: seen.append(e.type.value))
    assert seen == types(ok)

    def broken(_event) -> None:
        raise RuntimeError("ui exploded")

    result = run(on_event=broken)
    assert result.state is S.VERIFIED and result.observer_errors == len(result.events)


def test_rescore_replays_exactly_the_first_pass_batch_ids() -> None:
    result = run()
    assert [t.mutation_id for t in result.rescore.after_report.trials] == [
        t.mutation_id for t in result.first_pass.trials
    ]


# ---------------------------------------------------------------- failed / stage boundaries


def test_red_baseline_fails_at_the_baseline_boundary() -> None:
    result = run(ScriptedExecutor(baseline=O.TEST_FAIL))
    assert (result.state, result.reason) == (S.FAILED, F.BASELINE_NOT_READY)
    assert stages(result) == [S.BASELINE] and result.stages[0].status == "terminal"
    assert result.plan is None and types(result)[-1] == "run.failed"
    assert result.events[-1].data["code"] == "baseline_not_ready"


@pytest.mark.parametrize("planner", [BrokenPlanner(), ExplodingPlanner()])
def test_planning_failure_stops_before_any_mutation_runs(planner) -> None:
    executor = ScriptedExecutor()
    result = run(executor, planner=planner)
    assert (result.state, result.reason) == (S.FAILED, F.PLANNING_FAILED)
    assert stages(result) == [S.BASELINE, S.CONTEXT, S.PLANNING]
    assert not any(c.startswith("mutation:") for c in executor.calls)


def test_context_failure_is_typed(monkeypatch) -> None:
    def boom(*a, **k):
        raise orchestrator.ContextError("no mutable files")

    monkeypatch.setattr(orchestrator, "pack_context", boom)
    result = run()
    assert (result.state, result.reason) == (S.FAILED, F.CONTEXT_FAILED)


def test_no_valid_mutation_outcomes_is_inconclusive_not_a_zero_score() -> None:
    executor = ScriptedExecutor(first_pass={f"M0{i}": O.INFRA_ERROR for i in range(1, 9)})
    result = run(executor)
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.NO_VALID_MUTATION_OUTCOMES)
    assert result.first_pass.score.score is None
    assert stages(result)[-1] is S.MUTATION_EXECUTION


def test_no_survivor_is_a_clean_terminal_state_without_model_calls() -> None:
    analyzer, generator = FakeAnalyzer(), FakeGenerator()
    executor = ScriptedExecutor(first_pass={f"M0{i}": O.TEST_FAIL for i in range(1, 9)})
    result = run(executor, analyzer=analyzer, generator=generator)
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.NO_ACTIONABLE_SURVIVOR)
    assert analyzer.requests == [] and generator.requests == []
    assert result.selection.status.value == "no_survivors" and types(result)[-1] == "run.completed"


def test_all_possibly_equivalent_is_inconclusive_and_retains_analyses() -> None:
    analyzer = FakeAnalyzer(equivalent={"M01", "M02", "M07"})
    generator = FakeGenerator()
    result = run(analyzer=analyzer, generator=generator)
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.ALL_POSSIBLY_EQUIVALENT)
    assert len(result.selection.analyses) == 3 and generator.requests == []
    assert types(result).count("analysis.completed") == 3


def test_analyzer_provider_failure_is_failed_at_analysis() -> None:
    result = run(analyzer=FakeAnalyzer(error=RuntimeError("quota")))
    assert (result.state, result.reason) == (S.FAILED, F.ANALYSIS_FAILED)
    assert stages(result)[-1] is S.SURVIVOR_ANALYSIS


@pytest.mark.parametrize(
    "generator",
    [FakeGenerator(error=RuntimeError("quota")), FakeGenerator(raw={"mutation_id": "M01"})],
)
def test_generator_failure_or_invalid_output_is_failed_at_generation(generator) -> None:
    result = run(generator=generator)
    assert (result.state, result.reason) == (S.FAILED, F.GENERATION_FAILED)
    assert stages(result)[-1] is S.TEST_GENERATION


# ---------------------------------------------------------------- rejected / inconclusive


@pytest.mark.parametrize(
    "generator",
    [
        FakeGenerator("def test_x(:\n    pass\n"),  # invalid syntax
        FakeGenerator(target="src/nowhere.py"),  # forbidden target
        FakeGenerator(target="../escape.py"),  # traversal
    ],
)
def test_invalid_candidate_is_rejected_before_verification(generator) -> None:
    executor = ScriptedExecutor()
    result = run(executor, generator=generator)
    assert (result.state, result.reason) == (S.REJECTED, F.CANDIDATE_INVALID)
    assert stages(result)[-1] is S.TEST_GENERATION and result.verification is None
    assert not any(c.startswith("mutation:original") for c in executor.calls)


def test_collection_failure_in_preflight_is_a_rejected_candidate() -> None:
    result = run(ScriptedExecutor(preflight=O.INVALID))
    assert (result.state, result.reason) == (S.REJECTED, F.CANDIDATE_INVALID)


def test_preflight_infrastructure_failure_is_inconclusive_not_a_rejection() -> None:
    result = run(ScriptedExecutor(preflight=O.INFRA_ERROR))
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.PREFLIGHT_UNAVAILABLE)


def test_candidate_failing_on_the_original_is_rejected() -> None:
    result = run(ScriptedExecutor(original=O.TEST_FAIL), generator=FakeGenerator(WRONG_TEST))
    assert (result.state, result.reason) == (S.REJECTED, F.CANDIDATE_FAILED_ORIGINAL)
    assert result.rescore is None and stages(result)[-1] is S.VERIFICATION


def test_mutant_that_still_passes_is_inconclusive_and_skips_rescore() -> None:
    executor = ScriptedExecutor(mutant=O.PASS)
    result = run(executor, generator=FakeGenerator(WEAK_TEST))
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.MUTANT_NOT_KILLED)
    assert result.rescore is None and result.verification.result.verdict == "inconclusive"
    assert not any(c.startswith("mutation:M") and "test_perjury" in c for c in executor.calls)


@pytest.mark.parametrize("original", [O.INVALID, O.TIMEOUT, O.INFRA_ERROR])
def test_unclean_original_world_is_inconclusive(original) -> None:
    result = run(ScriptedExecutor(original=original))
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.VERIFICATION_INCONCLUSIVE)


@pytest.mark.parametrize("mutant", [O.INVALID, O.TIMEOUT, O.INFRA_ERROR])
def test_excluded_mutant_world_can_never_verify(mutant) -> None:
    result = run(ScriptedExecutor(mutant=mutant))
    assert result.state is S.INCONCLUSIVE and result.reason is F.MUTANT_NOT_KILLED


def test_inconsistent_worlds_fail_the_run(monkeypatch) -> None:
    def broken(*a, **k):
        raise orchestrator.WorldConsistencyError("worlds differ")

    monkeypatch.setattr(orchestrator, "verify_candidate", broken)
    result = run()
    assert (result.state, result.reason) == (S.FAILED, F.WORLD_INCONSISTENT)


def test_inconsistent_replay_is_inconclusive_and_claims_no_improvement() -> None:
    executor = ScriptedExecutor(rescore=dict(ScriptedExecutor().first_pass))  # M01 still survives
    result = run(executor)
    assert (result.state, result.reason) == (S.INCONCLUSIVE, F.RESCORE_INCONSISTENT)
    assert result.rescore is not None and not result.improvement_verified
    assert result.verification.result.verdict == "verified"  # candidate verdict is retained


def test_unexpected_exception_ends_in_a_typed_failed_state(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(orchestrator, "select_hardening_target", boom)
    result = run()
    assert (result.state, result.reason) == (S.FAILED, F.UNEXPECTED_ERROR)
    assert "kaboom" in result.message and result.stages[-1].status == "failed"
    assert result.events[-1].type is RunEventType.RUN_FAILED


def test_config_bounds_concurrency() -> None:
    RunConfig(max_concurrency=10)
    with pytest.raises(ValueError):
        RunConfig(max_concurrency=11)
    assert run(config=RunConfig(max_concurrency=2)).first_pass.max_concurrency == 2


def test_premium_test_constant_is_a_valid_candidate() -> None:
    assert "premium=True" in PREMIUM_TEST
