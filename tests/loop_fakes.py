"""Deterministic fakes for the model and execution boundaries of the closed loop."""

from __future__ import annotations

from typing import Any

from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import ExecutionResult, SurvivorAnalysis
from perjury.contracts import TestProposal as GeneratedTest

PREMIUM_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_premium_customer_outside_window_still_gets_refund() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
)
WEAK_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_inside_window() -> None:\n"
    "    assert calculate_refund(50.0, 5, premium=False) == 50.0\n"
)
WRONG_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_wrong() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 0.0\n"
)
TARGET_FILE = "examples/refund/test_refund.py"


class FakeAnalyzer:
    def __init__(self, equivalent: set[str] | None = None, error: Exception | None = None) -> None:
        self.equivalent = equivalent or set()
        self.error = error
        self.requests: list[str] = []

    def analyze(self, request: Any):
        self.requests.append(request.mutation_id)
        if self.error:
            raise self.error
        return SurvivorAnalysis(
            mutation_id=request.mutation_id,
            behavioural_gap="No test covers premium customers outside the 30-day window.",
            test_intent="Premium customers past day 30 still receive a full refund.",
            possibly_equivalent=request.mutation_id in self.equivalent,
            reasoning="Removing 'or premium' changes results only for premium and days > 30.",
        )


class FakeGenerator:
    def __init__(
        self,
        code: str = PREMIUM_TEST,
        *,
        target: str = TARGET_FILE,
        error: Exception | None = None,
        raw: Any = None,
    ) -> None:
        self.code, self.target, self.error, self.raw = code, target, error, raw
        self.requests: list[str] = []

    def generate(self, request: Any):
        self.requests.append(request.mutation_id)
        if self.error:
            raise self.error
        if self.raw is not None:
            return self.raw
        return GeneratedTest(
            mutation_id=request.mutation_id,
            test_name="test_premium_customer_outside_window_still_gets_refund",
            target_file=self.target,
            test_code=self.code,
            explanation="Pins the premium out-of-window refund rule.",
        )


CANONICAL_FIRST_PASS = {
    "M01": O.PASS,
    "M02": O.PASS,
    "M03": O.TEST_FAIL,
    "M04": O.TEST_FAIL,
    "M05": O.TEST_FAIL,
    "M06": O.TEST_FAIL,
    "M07": O.PASS,
    "M08": O.TEST_FAIL,
}


def _result(eid: str, outcome: O) -> ExecutionResult:
    code = {O.PASS: 0, O.TEST_FAIL: 1, O.INVALID: 2}.get(outcome)
    return ExecutionResult(
        execution_id=eid,
        outcome=outcome,
        exit_code=code,
        duration_ms=4,
        stdout="1 passed" if outcome is O.PASS else "",
        failure_detail=None if code is not None else f"{outcome.value} (scripted)",
    )


class ScriptedExecutor:
    """Scripted WorkspaceExecutor keyed by the execution-id conventions of each stage."""

    def __init__(
        self,
        *,
        baseline: O = O.PASS,
        first_pass: dict[str, O] | None = None,
        preflight: O = O.PASS,
        original: O = O.PASS,
        mutant: O = O.TEST_FAIL,
        rescore: dict[str, O] | None = None,
    ) -> None:
        self.baseline = baseline
        self.first_pass = first_pass or dict(CANONICAL_FIRST_PASS)
        self.preflight = preflight
        self.original = original
        self.mutant = mutant
        self.rescore = rescore or {**self.first_pass, "M01": O.TEST_FAIL}
        self.calls: list[str] = []

    def execute(self, spec, *, execution_id, manifest=None):
        self.calls.append(execution_id)
        if execution_id.startswith("baseline:"):
            return _result(execution_id, self.baseline)
        if execution_id.startswith("candidate-preflight:"):
            return _result(execution_id, self.preflight)
        if execution_id.startswith("mutation:original:"):
            return _result(execution_id, self.original)
        if execution_id.startswith("mutation:mutant:"):
            return _result(execution_id, self.mutant)
        mutation_id = execution_id.removeprefix("mutation:")
        has_candidate = any("test_perjury" in arg for arg in spec.pytest_argv)
        table = self.rescore if has_candidate else self.first_pass
        return _result(execution_id, table[mutation_id])
