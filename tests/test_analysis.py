from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from perjury.analysis import (
    POTENTIAL_TEST_GAP,
    AnalysisFailureCode,
    AnalysisRequest,
    SelectionStatus,
    SurvivorAnalysisError,
    SurvivorSelection,
    mutation_id_sort_key,
    select_hardening_target,
)
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome, ExecutionResult, SurvivorAnalysis
from perjury.execution import MutationExecutionReport, execute_mutation_plan
from perjury.planning import MutationPlan, plan_mutations
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]
PASS, FAIL = ExecutionOutcome.PASS, ExecutionOutcome.TEST_FAIL


class ScriptedExecutor:
    def __init__(self, survivors: set[str], invalid: set[str] = frozenset()) -> None:
        self.survivors, self.invalid = survivors, invalid

    def execute(self, spec, *, execution_id, manifest=None):
        mid = execution_id.removeprefix("mutation:")
        if mid in self.invalid:
            return ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.INVALID,
                duration_ms=1,
                failure_detail="x",
            )
        code, outcome = (0, PASS) if mid in self.survivors else (1, FAIL)
        return ExecutionResult(
            execution_id=execution_id, outcome=outcome, exit_code=code, duration_ms=1
        )


class FakeAnalyzer:
    def __init__(self, equivalent: set[str] = frozenset(), overrides: dict[str, Any] | None = None):
        self.equivalent = equivalent
        self.overrides = overrides or {}
        self.requests: list[AnalysisRequest] = []

    def analyze(self, request: AnalysisRequest):
        self.requests.append(request)
        if request.mutation_id in self.overrides:
            value = self.overrides[request.mutation_id]
            if isinstance(value, Exception):
                raise value
            return value
        return SurvivorAnalysis(
            mutation_id=request.mutation_id,
            behavioural_gap=f"gap for {request.mutation_id}",
            test_intent=f"intent for {request.mutation_id}",
            possibly_equivalent=request.mutation_id in self.equivalent,
            reasoning="because",
        )


@pytest.fixture(scope="module")
def world():
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    return spec, baseline, plan


def report_for(world, survivors: set[str], invalid: set[str] = frozenset()):
    spec, baseline, plan = world
    return execute_mutation_plan(spec, baseline, plan, ScriptedExecutor(survivors, invalid))


def select(world, survivors, analyzer, invalid=frozenset()):
    spec, baseline, plan = world
    return select_hardening_target(
        spec, baseline, plan, report_for(world, survivors, invalid), analyzer
    )


def test_only_survivors_are_candidates_in_stable_id_order(world) -> None:
    analyzer = FakeAnalyzer(equivalent={"M02", "M05", "M07", "M08"})
    result = select(world, {"M07", "M02", "M08", "M05"}, analyzer, invalid={"M03"})
    assert result.candidate_ids == ("M02", "M05", "M07", "M08")
    assert [r.mutation_id for r in analyzer.requests] == ["M02", "M05", "M07", "M08"]
    assert "M03" not in result.candidate_ids and "M01" not in result.candidate_ids


def test_id_ordering_is_numeric_not_lexicographic() -> None:
    ids = ["M100", "M9", "M10", "M02"]
    assert sorted(ids, key=mutation_id_sort_key) == ["M02", "M9", "M10", "M100"]


def test_first_non_equivalent_survivor_is_selected_and_equivalents_are_skipped(world) -> None:
    analyzer = FakeAnalyzer(equivalent={"M02", "M07"})
    result = select(world, {"M02", "M07", "M08", "M05"}, analyzer)
    assert result.status is SelectionStatus.SELECTED
    assert result.target and result.target.mutation_id == "M05"
    assert result.skipped_equivalent_ids == ("M02",)
    assert [a.mutation_id for a in result.analyses] == ["M02", "M05"]
    # later survivors are not analysed once a target is found (bounded, first-suitable policy)
    assert [r.mutation_id for r in analyzer.requests] == ["M02", "M05"]
    assert result.analyses[0].possibly_equivalent is True  # skipped analysis retained as evidence


def test_selection_is_reproducible_regardless_of_input_order(world) -> None:
    runs = [
        select(world, survivors, FakeAnalyzer(equivalent={"M01"}))
        for survivors in ({"M01", "M02", "M07"}, {"M07", "M02", "M01"})
    ]
    assert runs[0].model_dump() == runs[1].model_dump()
    assert runs[0].target.mutation_id == "M02"


def test_target_carries_diff_path_and_potential_test_gap_language(world) -> None:
    result = select(world, {"M01"}, FakeAnalyzer())
    target = result.target
    assert target.label == POTENTIAL_TEST_GAP == "potential test gap"
    assert target.summary.startswith("Potential test gap in examples/refund/refund.py")
    assert "or premium" in target.diff and target.target_path == "examples/refund/refund.py"
    for text in (target.summary, result.message):
        assert "confirmed bug" not in text.lower()
    assert "potential test gap" in result.message


def test_prompt_frames_survivor_as_potential_gap_with_untrusted_delimiting(world) -> None:
    analyzer = FakeAnalyzer()
    select(world, {"M01"}, analyzer)
    prompt = analyzer.requests[0].prompt
    lowered = prompt.lower()
    assert "potential test gap" in lowered and "never call it a bug" in lowered
    begin = prompt.index("=== BEGIN UNTRUSTED REPOSITORY CONTENT")
    end = prompt.index("=== END UNTRUSTED REPOSITORY CONTENT")
    assert prompt.index("or premium") > begin  # first occurrence is inside the delimiters
    assert begin < prompt.index("description: Premium customers lose") < end
    assert "Return mutation_id exactly 'M01'" in prompt


def test_no_survivors_is_a_clean_typed_outcome_without_model_calls(world) -> None:
    analyzer = FakeAnalyzer()
    result = select(world, set(), analyzer)
    assert result.status is SelectionStatus.NO_SURVIVORS
    assert result.target is None and result.analyses == () and result.candidate_ids == ()
    assert analyzer.requests == []
    assert "no actionable" in result.message.lower()


def test_only_invalid_or_excluded_trials_are_not_survivors(world) -> None:
    result = select(world, set(), FakeAnalyzer(), invalid={"M01", "M02"})
    assert result.status is SelectionStatus.NO_SURVIVORS


def test_all_possibly_equivalent_is_a_clean_typed_outcome_with_evidence(world) -> None:
    result = select(world, {"M02", "M07"}, FakeAnalyzer(equivalent={"M02", "M07"}))
    assert result.status is SelectionStatus.ALL_POSSIBLY_EQUIVALENT
    assert result.target is None
    assert result.skipped_equivalent_ids == ("M02", "M07")
    assert [a.mutation_id for a in result.analyses] == ["M02", "M07"]
    assert "not proven" in result.message


def test_dict_analysis_is_validated_like_a_live_result(world) -> None:
    raw = {
        "mutation_id": "M01",
        "behavioural_gap": "g",
        "test_intent": "t",
        "possibly_equivalent": False,
        "reasoning": "r",
    }
    result = select(world, {"M01"}, FakeAnalyzer(overrides={"M01": raw}))
    assert isinstance(result.analyses[0], SurvivorAnalysis)


@pytest.mark.parametrize(
    "override,code",
    [
        ({"mutation_id": "M01"}, AnalysisFailureCode.INVALID_OUTPUT),  # missing fields
        (
            SurvivorAnalysis(
                mutation_id="M99", behavioural_gap="g", test_intent="t", reasoning="r"
            ),
            AnalysisFailureCode.INVALID_OUTPUT,  # wrong id
        ),
        (RuntimeError("quota"), AnalysisFailureCode.PROVIDER_ERROR),
    ],
)
def test_invalid_analysis_or_provider_failure_is_typed(world, override, code) -> None:
    with pytest.raises(SurvivorAnalysisError) as caught:
        select(world, {"M01"}, FakeAnalyzer(overrides={"M01": override}))
    assert caught.value.code is code


def test_failure_after_partial_progress_keeps_earlier_analyses(world) -> None:
    analyzer = FakeAnalyzer(equivalent={"M01"}, overrides={"M02": RuntimeError("boom")})
    with pytest.raises(SurvivorAnalysisError) as caught:
        select(world, {"M01", "M02"}, analyzer)
    assert [a.mutation_id for a in caught.value.analyses] == ["M01"]


def test_report_from_a_different_plan_is_rejected(world) -> None:
    spec, baseline, plan = world
    report = report_for(world, {"M01"})
    forged = MutationExecutionReport.model_validate(
        {**report.model_dump(), "context_sha256": "sha256:" + "0" * 64}
    )
    with pytest.raises(SurvivorAnalysisError) as caught:
        select_hardening_target(spec, baseline, plan, forged, FakeAnalyzer())
    assert caught.value.code is AnalysisFailureCode.INCONSISTENT_INPUTS
    short_plan = MutationPlan.model_validate(plan.model_dump())
    short = report.model_copy(update={"trials": report.trials[:-1]})
    with pytest.raises(SurvivorAnalysisError):
        select_hardening_target(spec, baseline, short_plan, short, FakeAnalyzer())


def test_selection_contract_rejects_inconsistent_outcomes(world) -> None:
    good = select(world, {"M01"}, FakeAnalyzer())
    data = good.model_dump()
    data["status"] = "no_survivors"
    with pytest.raises(ValueError):
        SurvivorSelection.model_validate(data)
    data = good.model_dump()
    data["analyses"][0]["possibly_equivalent"] = True
    data["target"]["analysis"]["possibly_equivalent"] = True
    with pytest.raises(ValueError):
        SurvivorSelection.model_validate(data)
    assert SurvivorSelection.model_validate_json(good.model_dump_json()) == good


def test_canonical_refund_selects_the_premium_survivor_with_real_pytest(world) -> None:
    spec, baseline, plan = world
    report = execute_mutation_plan(spec, baseline, plan, LocalPytestExecutor(), max_concurrency=4)
    result = select_hardening_target(spec, baseline, plan, report, FakeAnalyzer())
    assert result.candidate_ids == ("M01", "M02", "M07")
    assert result.target.mutation_id == "M01"


def test_importing_analysis_does_not_construct_the_live_agent() -> None:
    code = "import sys, perjury.analysis; sys.exit('perjury.agent' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
