from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

import modal

from .contracts import ExecutionResult, MutationStatus

PYTEST_VERSION = "9.1.1"


@lru_cache(maxsize=1)
def _get_app() -> modal.App:
    """Resolve the persisted Modal app only when live execution begins."""
    return modal.App.lookup("perjury", create_if_missing=True)


@lru_cache(maxsize=1)
def _get_runtime() -> modal.Image:
    """Construct the pinned hackathon runtime lazily."""
    return modal.Image.debian_slim(python_version="3.12").pip_install(
        f"pytest==${PYTEST_VERSION}"
    )


@dataclass(slots=True)
class RunSpec:
    mutation_id: str
    command: tuple[str, ...] = ("pytest", "-q")
    workspace_files: dict[str, str] = field(default_factory=dict)
    workspace: str = "/workspace"


def classify_exit_code(code: int) -> MutationStatus:
    # Temporary mutation projection until #8 lands the richer execution taxonomy.
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

    Modal resolution is deliberately lazy so importing PERJURY remains offline-safe.
    Commands remain structured argv values; workdir is supplied directly to
    Sandbox.exec rather than constructing a shell command.
    """
    started = time.perf_counter()

    sandbox = modal.Sandbox.create(
        app=_get_app(),
        image=_get_runtime(),
        timeout=120,
    )
    try:
        for relative_path, contents in spec.workspace_files.items():
            sandbox.filesystem.write_text(
                contents,
                _remote_path(spec.workspace, relative_path),
            )

        process = sandbox.exec(
            *spec.command,
            workdir=spec.workspace if spec.workspace_files else None,
            timeout=120,
        )
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
