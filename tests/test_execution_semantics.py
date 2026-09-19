import pytest

from perjury.contracts import (
    ExecutionOutcome,
    ExecutionResult,
    MutationStatus,
    execution_outcome_for_pytest_exit,
    mutation_status_for,
)
from perjury.modal_runner import (
    RunSpec,
    _is_pytest_command,
    classify_pytest_exit_code,
    execute_pytest,
)


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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("pytest", "-q"), True),
        (("python", "-m", "pytest", "-q"), True),
        (("python3", "-m", "pytest"), True),
        (("python", "-c", "print('x')"), False),
        (("bash", "-lc", "pytest"), False),
        ((), False),
    ],
)
def test_pytest_command_detection(
    command: tuple[str, ...],
    expected: bool,
) -> None:
    assert _is_pytest_command(command) is expected


def test_execute_pytest_rejects_non_pytest_command_without_modal() -> None:
    result = execute_pytest(
        RunSpec(
            mutation_id="M99",
            command=("python", "-c", "raise SystemExit(1)"),
        )
    )

    assert result.outcome is ExecutionOutcome.INVALID
    assert result.exit_code is None


@pytest.mark.parametrize(
    ("workspace", "relative_path"),
    [
        ("/workspace", "../escape.py"),
        ("/workspace", "/absolute.py"),
        ("/workspace", "."),
        ("/", "tmp/escape.py"),
        ("/tmp", "test.py"),
    ],
)
def test_invalid_workspace_path_is_rejected_before_modal(
    workspace: str,
    relative_path: str,
) -> None:
    result = execute_pytest(
        RunSpec(
            mutation_id="M98",
            workspace=workspace,
            workspace_files={relative_path: "def test_x(): assert True\n"},
            command=("pytest", "-q"),
        )
    )

    assert result.outcome is ExecutionOutcome.INVALID
    assert result.exit_code is None


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
def test_shared_contract_exit_mapping(
    exit_code: int,
    expected: ExecutionOutcome,
) -> None:
    assert execution_outcome_for_pytest_exit(exit_code) is expected


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [
        (ExecutionOutcome.INVALID, 0),
        (ExecutionOutcome.INFRA_ERROR, 1),
        (ExecutionOutcome.PASS, None),
        (ExecutionOutcome.TEST_FAIL, None),
    ],
)
def test_execution_result_rejects_contradictory_outcome_and_exit_code(
    outcome: ExecutionOutcome,
    exit_code: int | None,
) -> None:
    with pytest.raises(ValueError):
        ExecutionResult(
            mutation_id="M97",
            outcome=outcome,
            exit_code=exit_code,
            duration_ms=1,
        )


def test_run_spec_environment_defaults_empty() -> None:
    spec = RunSpec(mutation_id="M96")
    assert spec.env == {}


def test_execution_result_keeps_cleanup_error_separate() -> None:
    result = ExecutionResult(
        mutation_id="M95",
        outcome=ExecutionOutcome.PASS,
        exit_code=0,
        duration_ms=1,
        cleanup_error="Sandbox cleanup failed",
    )

    assert result.outcome is ExecutionOutcome.PASS
    assert result.cleanup_error == "Sandbox cleanup failed"
