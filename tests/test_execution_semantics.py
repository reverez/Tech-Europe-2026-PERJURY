import pytest

from perjury.contracts import ExecutionOutcome, MutationStatus, mutation_status_for
from perjury.modal_runner import classify_pytest_exit_code


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, ExecutionOutcome.PASS),
        (1, ExecutionOutcome.TEST_FAIL),
        (2, ExecutionOutcome.INVALID),
        (3, ExecutionOutcome.INFRA_ERROR),
        (4, ExecutionOutcome.INVALID),
        (5, ExecutionOutcome.INVALID),
        (127, ExecutionOutcome.INFRA_ERROR),
    ],
)
def test_pytest_exit_code_mapping(
    exit_code: int,
    expected: ExecutionOutcome,
) -> None:
    assert classify_pytest_exit_code(exit_code) is expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ExecutionOutcome.PASS, MutationStatus.SURVIVED),
        (ExecutionOutcome.TEST_FAIL, MutationStatus.KILLED),
        (ExecutionOutcome.INVALID, MutationStatus.INVALID),
        (ExecutionOutcome.TIMEOUT, MutationStatus.TIMEOUT),
        (ExecutionOutcome.INFRA_ERROR, MutationStatus.INFRA_ERROR),
    ],
)
def test_mutation_status_projection(
    outcome: ExecutionOutcome,
    expected: MutationStatus,
) -> None:
    assert mutation_status_for(outcome) is expected
