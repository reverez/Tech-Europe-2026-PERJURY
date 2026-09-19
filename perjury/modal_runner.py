from __future__ import annotations

import time
from dataclasses import dataclass

import modal

from .contracts import ExecutionResult, MutationStatus


app = modal.App("perjury")
runtime = modal.Image.debian_slim(python_version="3.12").pip_install("pytest>=8.4")


@dataclass(slots=True)
class RunSpec:
    mutation_id: str
    command: tuple[str, ...] = ("pytest", "-q")


def classify_exit_code(code: int) -> MutationStatus:
    # pytest: 0 = all tests passed, 1 = tests failed.
    if code == 0:
        return MutationStatus.SURVIVED
    if code == 1:
        return MutationStatus.KILLED
    return MutationStatus.INVALID


def execute_pytest(spec: RunSpec) -> ExecutionResult:
    """Minimal Modal execution primitive.

    The hackathon spike should exercise this boundary first. The production path will
    mount/copy a workspace, apply a mutation, run pytest, and return this typed result.
    """
    started = time.perf_counter()

    sandbox = modal.Sandbox.create(
        app=app,
        image=runtime,
        timeout=120,
    )
    try:
        process = sandbox.exec(*spec.command)
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.wait()
        code = process.returncode
    finally:
        sandbox.terminate()

    return ExecutionResult(
        mutation_id=spec.mutation_id,
        status=classify_exit_code(code),
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
