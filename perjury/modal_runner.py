from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import modal

from .contracts import (
    ExecutionOutcome,
    ExecutionResult,
    NetworkPolicy,
    SnapshotManifest,
    WorkspaceSpec,
    execution_outcome_for_pytest_exit,
    is_pytest_command,
)
from .workspace import SnapshotPolicyError, bound_execution_output, build_snapshot_manifest

PYTEST_VERSION = "9.1.1"
REMOTE_WORKSPACE = PurePosixPath("/workspace")


class SnapshotMaterializationError(RuntimeError):
    """Raised when source bytes no longer match the sanitized manifest."""


class SandboxFilesystem(Protocol):
    def make_directory(self, remote_path: str, *, create_parents: bool = True) -> None: ...

    def write_bytes(self, data: bytes, remote_path: str) -> None: ...


class SandboxProcess(Protocol):
    stdout: Any
    stderr: Any
    returncode: int | None

    def wait(self) -> int: ...


class SandboxHandle(Protocol):
    filesystem: SandboxFilesystem

    def exec(self, *args: str, **kwargs: object) -> SandboxProcess: ...

    def terminate(self, *, wait: bool = False) -> int | None: ...


SandboxFactory = Callable[..., SandboxHandle]


_INIT_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _get_app_unlocked() -> modal.App:
    """Resolve the persisted Modal app only when live execution begins."""
    return modal.App.lookup("perjury", create_if_missing=True)


def _get_app() -> modal.App:
    """Thread-safe: concurrent fan-out workers must not race the first lookup."""
    with _INIT_LOCK:
        return _get_app_unlocked()


@lru_cache(maxsize=1)
def _get_runtime_unlocked() -> modal.Image:
    """Construct the pinned hackathon runtime lazily."""
    return modal.Image.debian_slim(python_version="3.12").pip_install(
        "pytest==" + PYTEST_VERSION
    )


def _get_runtime() -> modal.Image:
    with _INIT_LOCK:
        return _get_runtime_unlocked()


def _create_sandbox(**kwargs: object) -> SandboxHandle:
    return modal.Sandbox.create(**kwargs)


def classify_pytest_exit_code(code: int) -> ExecutionOutcome:
    """Compatibility wrapper around the shared contract mapping."""
    return execution_outcome_for_pytest_exit(code)


def _is_pytest_command(command: tuple[str, ...]) -> bool:
    """Compatibility wrapper retained for deterministic callers/tests."""
    return is_pytest_command(command)


def _remote_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if str(path) in {"", "."} or path.is_absolute() or ".." in path.parts:
        raise SnapshotMaterializationError(
            f"snapshot file must be a safe relative path: {relative_path!r}"
        )
    return str(REMOTE_WORKSPACE / path)


def _local_manifest_file(root: Path, relative_path: str) -> Path:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise SnapshotMaterializationError(
                f"snapshot path became a symlink before upload: {relative_path!r}"
            )

    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise SnapshotMaterializationError(
            f"snapshot path no longer resolves inside source_root: {relative_path!r}"
        ) from exc
    if not resolved.is_file():
        raise SnapshotMaterializationError(
            f"snapshot path is no longer a regular file: {relative_path!r}"
        )
    return resolved


def _materialized_files(
    spec: WorkspaceSpec,
    manifest: SnapshotManifest,
) -> list[tuple[bytes, str]]:
    if manifest.workspace_id != spec.workspace_id:
        raise SnapshotMaterializationError("manifest workspace_id does not match WorkspaceSpec")
    if manifest.source_snapshot_id != spec.snapshot_id:
        raise SnapshotMaterializationError("manifest snapshot identity does not match WorkspaceSpec")

    root = Path(spec.source_root).expanduser().resolve()
    current_manifest = build_snapshot_manifest(spec)
    if current_manifest.manifest_sha256 != manifest.manifest_sha256:
        raise SnapshotMaterializationError(
            "source snapshot does not match the manifest supplied for Modal execution"
        )

    materialized: list[tuple[bytes, str]] = []
    for entry in manifest.files:
        data = _local_manifest_file(root, entry.path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != entry.size_bytes or digest != entry.sha256:
            raise SnapshotMaterializationError(
                f"snapshot file drifted before upload: {entry.path!r}"
            )
        materialized.append((data, _remote_path(entry.path)))
    return materialized


def _remote_working_directory(spec: WorkspaceSpec) -> str:
    root = Path(spec.source_root).expanduser().resolve()
    local = (root / spec.working_directory).resolve()
    try:
        local.relative_to(root)
    except ValueError as exc:
        raise SnapshotMaterializationError("working_directory escapes source_root") from exc
    if not local.is_dir():
        raise SnapshotMaterializationError(
            f"working_directory does not exist: {spec.working_directory!r}"
        )
    if spec.working_directory == ".":
        return str(REMOTE_WORKSPACE)
    return str(REMOTE_WORKSPACE / PurePosixPath(spec.working_directory))


def _read_process(process: SandboxProcess) -> tuple[int, str, str]:
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    return_code = process.wait()
    return return_code, stdout, stderr


class ModalWorkspaceExecutor:
    """Execute an exact sanitized WorkspaceSpec snapshot in a Modal Sandbox."""

    def __init__(
        self,
        *,
        app_factory: Callable[[], object] = _get_app,
        image_factory: Callable[[], object] = _get_runtime,
        sandbox_factory: SandboxFactory = _create_sandbox,
    ) -> None:
        self._app_factory = app_factory
        self._image_factory = image_factory
        self._sandbox_factory = sandbox_factory

    def execute(
        self,
        spec: WorkspaceSpec,
        *,
        execution_id: str,
        manifest: SnapshotManifest | None = None,
    ) -> ExecutionResult:
        started = time.perf_counter()
        active_manifest = manifest
        try:
            if active_manifest is None:
                active_manifest = build_snapshot_manifest(spec)
            files = _materialized_files(spec, active_manifest)
            workdir = _remote_working_directory(spec)
        except (SnapshotMaterializationError, SnapshotPolicyError, OSError) as exc:
            return ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.INVALID,
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_detail=str(exc),
            )

        sandbox: SandboxHandle | None = None
        outcome = ExecutionOutcome.INFRA_ERROR
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        failure_detail: str | None = None
        cleanup_error: str | None = None

        try:
            command_count = 2 if spec.install_argv is not None else 1
            sandbox = self._sandbox_factory(
                app=self._app_factory(),
                image=self._image_factory(),
                env=dict(spec.environment),
                secrets=(),
                include_oidc_identity_token=False,
                timeout=(spec.command_timeout_seconds * command_count) + 30,
                block_network=spec.network_policy is NetworkPolicy.BLOCKED,
            )
            sandbox.filesystem.make_directory(str(REMOTE_WORKSPACE))
            sandbox.filesystem.make_directory(workdir)
            for data, remote_path in files:
                sandbox.filesystem.write_bytes(data, remote_path)

            if spec.install_argv is not None:
                install = sandbox.exec(
                    *spec.install_argv,
                    workdir=workdir,
                    timeout=spec.command_timeout_seconds,
                    env=dict(spec.environment),
                    secrets=(),
                )
                install_code, stdout, stderr = _read_process(install)
                if install_code != 0:
                    failure_detail = f"install command failed with exit code {install_code}"
                else:
                    stdout = ""
                    stderr = ""

            if failure_detail is None:
                process = sandbox.exec(
                    *spec.pytest_argv,
                    workdir=workdir,
                    timeout=spec.command_timeout_seconds,
                    env=dict(spec.environment),
                    secrets=(),
                )
                exit_code, stdout, stderr = _read_process(process)
                outcome = classify_pytest_exit_code(exit_code)
        except modal.exception.TimeoutError as exc:
            outcome = ExecutionOutcome.TIMEOUT
            exit_code = None
            failure_detail = (
                f"command exceeded timeout={spec.command_timeout_seconds}s: {exc}"
            )
        except modal.Error as exc:
            outcome = ExecutionOutcome.INFRA_ERROR
            exit_code = None
            failure_detail = f"Modal provider failure: {exc}"
        except Exception as exc:  # noqa: BLE001 - provider boundary must still clean up
            outcome = ExecutionOutcome.INFRA_ERROR
            exit_code = None
            failure_detail = f"Sandbox execution failure: {exc}"
        finally:
            if sandbox is not None:
                try:
                    sandbox.terminate(wait=True)
                except Exception as exc:  # noqa: BLE001 - preserve the primary result
                    cleanup_error = f"Sandbox cleanup failed: {exc}"

        result = ExecutionResult(
            execution_id=execution_id,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failure_detail=failure_detail,
            cleanup_error=cleanup_error,
        )
        return bound_execution_output(spec, result)


@dataclass(slots=True)
class RunSpec:
    """Compatibility input for standalone smoke scripts.

    Production baseline/mutation paths use WorkspaceSpec + ModalWorkspaceExecutor directly.
    """

    execution_id: str
    command: tuple[str, ...] = ("pytest", "-q")
    workspace_files: dict[str, str] = field(default_factory=dict)
    workspace: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)


def execute_pytest(spec: RunSpec) -> ExecutionResult:
    """Execute legacy in-memory smoke files through the sanitized workspace path."""
    started = time.perf_counter()
    if not _is_pytest_command(spec.command):
        return ExecutionResult(
            execution_id=spec.execution_id,
            outcome=ExecutionOutcome.INVALID,
            failure_detail="execute_pytest accepts only pytest or python -m pytest commands.",
            duration_ms=0,
        )
    if spec.workspace != str(REMOTE_WORKSPACE):
        return ExecutionResult(
            execution_id=spec.execution_id,
            outcome=ExecutionOutcome.INVALID,
            failure_detail="MVP sandbox workspace must be exactly '/workspace'.",
            duration_ms=0,
        )

    try:
        paths = tuple(spec.workspace_files)
        for path in paths:
            _remote_path(path)
    except SnapshotMaterializationError as exc:
        return ExecutionResult(
            execution_id=spec.execution_id,
            outcome=ExecutionOutcome.INVALID,
            failure_detail=str(exc),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    temporary_root = Path(tempfile.mkdtemp(prefix="perjury-modal-smoke-"))
    try:
        for relative_path, contents in spec.workspace_files.items():
            path = temporary_root / PurePosixPath(relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")

        marker = "_perjury_smoke_implementation.py"
        if marker in spec.workspace_files:
            marker = "_perjury_smoke_implementation_2.py"
        (temporary_root / marker).write_text("SMOKE = True\n", encoding="utf-8")
        context_paths = paths
        if not context_paths:
            context_marker = "_perjury_smoke_context.py"
            (temporary_root / context_marker).write_text("CONTEXT = True\n", encoding="utf-8")
            context_paths = (context_marker,)

        identity = hashlib.sha256(spec.execution_id.encode("utf-8")).hexdigest()[:16]
        workspace_spec = WorkspaceSpec(
            workspace_id=f"legacy-{identity}",
            source_root=str(temporary_root),
            snapshot_id=f"legacy:{identity}",
            pytest_argv=spec.command,
            mutable_paths=(marker,),
            context_paths=context_paths,
            environment=spec.env,
        )
        manifest = build_snapshot_manifest(workspace_spec)
        return ModalWorkspaceExecutor().execute(
            workspace_spec,
            execution_id=spec.execution_id,
            manifest=manifest,
        )
    except (SnapshotPolicyError, ValueError) as exc:
        return ExecutionResult(
            execution_id=spec.execution_id,
            outcome=ExecutionOutcome.INVALID,
            failure_detail=str(exc),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
