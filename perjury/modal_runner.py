from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

import modal

from .contracts import ExecutionOutcome, ExecutionResult, execution_outcome_for_pytest_exit

PYTEST_VERSION = "9.1.1"


@lru_cache(maxsize=1)
def _get_app() -> modal.App:
    """Resolve the persisted Modal app only when live execution begins."""
    return modal.App.lookup("perjury", create_if_missing=True)


@lru_cache(maxsize=1)
def _get_runtime() -> modal.Image:
    """Construct the pinned hackathon runtime lazily."""
    return modal.Image.debian_slim(python_version="3.12").pip_install(
        "pytest==" + PYTEST_VERSION
    )


@dataclass(slots=True)
class RunSpec:
    mutation_id: str
    command: tuple[str, ...] = ("pytest", "-q")
    workspace_files: dict[str, str] = field(default_factory=dict)
    workspace: str = "/workspace"


def _is_pytest_command(command: tuple[str, ...]) -> bool:
    return bool(command) and (
        command[0] == "pytest"
        or (
            len(command) >= 3
            and command[0] in {"python", "python3"}
            and command[1:3] == ("-m", "pytest")
        )
    )


def classify_pytest_exit_code(code: int) -> ExecutionOutcome:
    """Compatibility wrapper around the shared contract mapping."""
    return execution_outcome_for_pytest_exit(code)


def _remote_path(workspace: str, relative_path: str) -> str:
    root = PurePosixPath(workspace)
    if root != PurePosixPath("/workspace"):
        raise ValueError("MVP sandbox workspace must be exactly '/workspace'.")

    path = PurePosixPath(relative_path)
    if str(path) in {"", "."} or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"workspace file must be a safe relative path: {relative_path!r}")
    return str(root / path)


def execute_pytest(spec: RunSpec) -> ExecutionResult:
    """Run one pytest command in an isolated Modal Sandbox.

    Modal resolution is deliberately lazy so importing PERJURY remains offline-safe.
    Commands remain structured argv values and execute with Modal's workdir option.
    """
    started = time.perf_counter()
    if not _is_pytest_command(spec.command):
        return ExecutionResult(
            mutation_id=spec.mutation_id,
            outcome=ExecutionOutcome.INVALID,
            exit_code=None,
            stderr="execute_pytest accepts only pytest or python -m pytest commands.",
            duration_ms=0,
        )

    try:
        materialized_files = [
            (contents, _remote_path(spec.workspace, relative_path))
            for relative_path, contents in spec.workspace_files.items()
        ]
    except ValueError as exc:
        return ExecutionResult(
            mutation_id=spec.mutation_id,
            outcome=ExecutionOutcome.INVALID,
            exit_code=None,
            stderr=str(exc),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    sandbox = None
    outcome = ExecutionOutcome.INFRA_ERROR
    exit_code = None
    stdout = ""
    stderr = ""

    try:
        sandbox = modal.Sandbox.create(
            app=_get_app(),
            image=_get_runtime(),
            timeout=120,
            block_network=True,
        )

        for contents, remote_path in materialized_files:
            sandbox.filesystem.write_text(contents, remote_path)

        process = sandbox.exec(
            *spec.command,
            workdir=spec.workspace if spec.workspace_files else None,
            timeout=120,
        )
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.wait()
        exit_code = process.returncode
        outcome = classify_pytest_exit_code(exit_code)
    except modal.exception.TimeoutError as exc:
        outcome = ExecutionOutcome.TIMEOUT
        stderr = str(exc)
    except modal.Error as exc:
        outcome = ExecutionOutcome.INFRA_ERROR
        stderr = str(exc)
    except ValueError as exc:
        outcome = ExecutionOutcome.INVALID
        stderr = str(exc)
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate(wait=True)
            except modal.Error as exc:
                outcome = ExecutionOutcome.INFRA_ERROR
                exit_code = None
                cleanup_error = f"Sandbox cleanup failed: {exc}"
                stderr = f"{stderr}\n{cleanup_error}".strip()

    return ExecutionResult(
        mutation_id=spec.mutation_id,
        outcome=outcome,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
