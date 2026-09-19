import pytest

from perjury.contracts import ExecutionOutcome, VerificationEvidence
from perjury.verification import judge_verification


def evidence(
    original: ExecutionOutcome,
    mutant: ExecutionOutcome,
) -> VerificationEvidence:
    exit_codes = {
        ExecutionOutcome.PASS: 0,
        ExecutionOutcome.TEST_FAIL: 1,
        ExecutionOutcome.INVALID: 4,
        ExecutionOutcome.TIMEOUT: None,
        ExecutionOutcome.INFRA_ERROR: 3,
    }
    return VerificationEvidence(
        mutation_id="M03",
        original_outcome=original,
        mutant_outcome=mutant,
        original_exit_code=exit_codes[original],
        mutant_exit_code=exit_codes[mutant],
        original_duration_ms=15,
        mutant_duration_ms=17,
    )


def test_accepts_only_original_pass_mutant_test_fail() -> None:
    result = judge_verification(
        evidence(ExecutionOutcome.PASS, ExecutionOutcome.TEST_FAIL),
        mutation_id="M03",
    )

    assert result.verdict == "verified"
    assert result.evidence.verified is True


def test_rejects_test_that_breaks_original() -> None:
    result = judge_verification(
        evidence(ExecutionOutcome.TEST_FAIL, ExecutionOutcome.TEST_FAIL),
        mutation_id="M03",
    )

    assert result.verdict == "rejected"


@pytest.mark.parametrize(
    "mutant_outcome",
    [
        ExecutionOutcome.PASS,
        ExecutionOutcome.INVALID,
        ExecutionOutcome.TIMEOUT,
        ExecutionOutcome.INFRA_ERROR,
    ],
)
def test_original_pass_without_mutant_test_failure_is_inconclusive(
    mutant_outcome: ExecutionOutcome,
) -> None:
    result = judge_verification(
        evidence(ExecutionOutcome.PASS, mutant_outcome),
        mutation_id="M03",
    )

    assert result.verdict == "inconclusive"
    assert result.evidence.verified is False


@pytest.mark.parametrize(
    "original_outcome",
    [
        ExecutionOutcome.INVALID,
        ExecutionOutcome.TIMEOUT,
        ExecutionOutcome.INFRA_ERROR,
    ],
)
def test_non_test_failure_in_original_world_is_inconclusive(
    original_outcome: ExecutionOutcome,
) -> None:
    result = judge_verification(
        evidence(original_outcome, ExecutionOutcome.TEST_FAIL),
        mutation_id="M03",
    )

    assert result.verdict == "inconclusive"


@pytest.mark.parametrize("original_outcome", list(ExecutionOutcome))
@pytest.mark.parametrize("mutant_outcome", list(ExecutionOutcome))
def test_complete_verdict_matrix(
    original_outcome: ExecutionOutcome,
    mutant_outcome: ExecutionOutcome,
) -> None:
    result = judge_verification(
        evidence(original_outcome, mutant_outcome),
        mutation_id="M03",
    )

    if (
        original_outcome is ExecutionOutcome.PASS
        and mutant_outcome is ExecutionOutcome.TEST_FAIL
    ):
        expected = "verified"
    elif original_outcome is ExecutionOutcome.TEST_FAIL:
        expected = "rejected"
    else:
        expected = "inconclusive"

    assert result.verdict == expected
