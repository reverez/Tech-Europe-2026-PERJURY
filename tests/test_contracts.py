import pytest
from pydantic import ValidationError

from perjury.contracts import (
    ExecutionOutcome,
    ExecutionResult,
    HardeningResult,
    MutationStatus,
    VerificationEvidence,
)


def test_verified_result_requires_two_sided_semantic_evidence() -> None:
    evidence = VerificationEvidence(
        mutation_id="M01",
        original_outcome=ExecutionOutcome.TEST_FAIL,
        mutant_outcome=ExecutionOutcome.TEST_FAIL,
        original_exit_code=1,
        mutant_exit_code=1,
        original_duration_ms=10,
        mutant_duration_ms=10,
    )

    with pytest.raises(ValidationError):
        HardeningResult(
            mutation_id="M01",
            verdict="verified",
            evidence=evidence,
            explanation="should not validate",
        )


def test_execution_result_rejects_contradictory_pass_exit_code() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(
            mutation_id="M01",
            outcome=ExecutionOutcome.PASS,
            exit_code=1,
            duration_ms=10,
        )


def test_mutation_status_is_derived_from_execution_outcome() -> None:
    result = ExecutionResult(
        mutation_id="M01",
        outcome=ExecutionOutcome.TEST_FAIL,
        exit_code=1,
        duration_ms=10,
    )

    assert result.status is MutationStatus.KILLED
