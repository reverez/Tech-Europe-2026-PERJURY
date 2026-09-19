from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import modal

from .contracts import ExecutionResult, MutationStatus

# A persisted app can be used directly by Sandbox.create from local code.
app = modal.App.lookup("perjury", create_if_missing=True)
runtime = modal.Image.debian_slim(python_version="3.12").pip_install("pytest>=8.4")


@dataclass(slots=True)
class RunSpec:
    mutation_id: str
    command: tuple[str, ...] = ("pytest", "-q")
    workspace_files: dict[str, str] = field(default_factory=dict)
    workspace: str = "/workspace"


def classify_exit_code(code: int) -> MutationStatus:
    # pytest: 0 = all tests passed, 1 = tests failed.
    if code == 0:
        return MutationStatus.SURVIVED
    if code == 1:
        return MutationStatus.KILLED
    return MutationStatus.INVALID


def _remote_path(workspace: str, relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"workspace file must be a safe relative path: {relative_path!r}")
    return str(PurePosixPath(workspace) / path)


def execute_pytest(spec: RunSpec) -> ExecutionResult:
    """Run one mutation candidate in an isolated Modal Sandbox.

    If workspace_files are supplied, they are materialized under /workspace and
    the command is executed from that directory. This is the core execution
    boundary used by the mutation and two-sided verification loops.
    """
    started = time.perf_counter()

    sandbox = modal.Sandbox.create(
        app=app,
        image=runtime,
        timeout=120,
    )
    try:
        for relative_path, contents in spec.workspace_files.items():
            sandbox.filesystem.write_text(
                contents,
                _remote_path(spec.workspace, relative_path),
            )

        if spec.workspace_files:
            shell_command = f"cd {shlex.quote(spec.workspace)} && {shlex.join(spec.command)}"
            process = sandbox.exec("bash", "-lc", shell_command)
        else:
            process = sandbox.exec(*spec.command)

        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.wait()
        code = process.returncode
    finally:
        sandbox.terminate(wait=True)

    return ExecutionResult(
        mutation_id=spec.mutation_id,
        status=classify_exit_code(code),
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
