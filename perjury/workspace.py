from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Protocol

from .contracts import (
    BaselineResult,
    ExecutionOutcome,
    ExecutionResult,
    SnapshotFile,
    SnapshotManifest,
    WorkspaceSpec,
    execution_outcome_for_pytest_exit,
)


class WorkspaceError(RuntimeError):
    """Base error for deterministic workspace preparation/execution."""


class SnapshotPolicyError(WorkspaceError):
    """Raised when a source workspace violates the snapshot safety policy."""


class BaselineNotReadyError(WorkspaceError):
    """Raised when baseline evidence does not permit mutation execution."""

    def __init__(self, result: BaselineResult) -> None:
        self.result = result
        detail = result.execution.failure_detail or result.execution.stderr
        message = (
            f"Baseline {result.execution.execution_id!r} is not ready for mutation: "
            f"outcome={result.execution.outcome.value}"
        )
        if result.manifest_sha256 != result.post_execution_manifest_sha256:
            message += (
                "; source snapshot changed during execution "
                f"({result.manifest_sha256} -> {result.post_execution_manifest_sha256})"
            )
        elif result.execution.cleanup_error:
            message += f"; cleanup_error={result.execution.cleanup_error}"
        elif detail:
            message += f"; detail={detail}"
        super().__init__(message)


class WorkspaceExecutor(Protocol):
    def execute(
        self,
        spec: WorkspaceSpec,
        *,
        execution_id: str,
    ) -> ExecutionResult:
        """Execute one configured pytest workspace and return semantic evidence."""


def _is_excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative_path)
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch.fnmatchcase(relative_path, pattern):
                return True
            continue
        if any(fnmatch.fnmatchcase(part, pattern) for part in path.parts):
            return True
    return False


def _allowlist_path_present(path: str, manifest_paths: set[str]) -> bool:
    prefix = f"{path.rstrip('/')}/"
    return path in manifest_paths or any(candidate.startswith(prefix) for candidate in manifest_paths)


def build_snapshot_manifest(spec: WorkspaceSpec) -> SnapshotManifest:
    """Build a deterministic sanitized file manifest without following symlinks."""
    root = Path(spec.source_root).expanduser().resolve()
    if not root.is_dir():
        raise SnapshotPolicyError(f"source_root is not a directory: {root}")

    files: list[SnapshotFile] = []
    total_bytes = 0
    manifest_digest = hashlib.sha256()

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)

        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            child = current / dirname
            relative = child.relative_to(root).as_posix()
            if _is_excluded(relative, spec.exclusion_patterns):
                continue
            if child.is_symlink():
                raise SnapshotPolicyError(
                    f"Symlinked directory is not supported in the MVP snapshot: {relative}"
                )
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(root).as_posix()
            if _is_excluded(relative, spec.exclusion_patterns):
                continue
            if path.is_symlink():
                raise SnapshotPolicyError(
                    f"Symlinked file is not supported in the MVP snapshot: {relative}"
                )
            if not path.is_file():
                continue

            data = path.read_bytes()
            size = len(data)
            if len(files) + 1 > spec.max_snapshot_files:
                raise SnapshotPolicyError(
                    f"Snapshot exceeds max_snapshot_files={spec.max_snapshot_files}."
                )
            if total_bytes + size > spec.max_snapshot_bytes:
                raise SnapshotPolicyError(
                    f"Snapshot exceeds max_snapshot_bytes={spec.max_snapshot_bytes}."
                )

            file_digest = hashlib.sha256(data).hexdigest()
            files.append(
                SnapshotFile(
                    path=relative,
                    size_bytes=size,
                    sha256=file_digest,
                )
            )
            total_bytes += size
            manifest_digest.update(relative.encode("utf-8"))
            manifest_digest.update(b"\0")
            manifest_digest.update(bytes.fromhex(file_digest))

    if not files:
        raise SnapshotPolicyError("Sanitized snapshot contains no files.")

    manifest_paths = {file.path for file in files}
    for allowed in (*spec.mutable_paths, *spec.context_paths):
        if not _allowlist_path_present(allowed, manifest_paths):
            raise SnapshotPolicyError(
                f"Workspace allowlist path is absent from sanitized snapshot: {allowed!r}"
            )

    return SnapshotManifest(
        workspace_id=spec.workspace_id,
        source_snapshot_id=spec.snapshot_id,
        manifest_sha256=f"sha256:{manifest_digest.hexdigest()}",
        files=tuple(files),
        total_bytes=total_bytes,
    )


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped, True


def bound_execution_output(spec: WorkspaceSpec, result: ExecutionResult) -> ExecutionResult:
    stdout, stdout_truncated = _truncate_utf8(result.stdout, spec.max_output_bytes)
    stderr, stderr_truncated = _truncate_utf8(result.stderr, spec.max_output_bytes)
    return result.model_copy(
        update={
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": result.stdout_truncated or stdout_truncated,
            "stderr_truncated": result.stderr_truncated or stderr_truncated,
        }
    )


def _normalize_python_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    executable = PurePosixPath(argv[0]).name.lower()
    if executable in {"pytest", "pytest.exe"}:
        return (sys.executable, "-m", "pytest", *argv[1:])
    if executable in {"pip", "pip3", "pip3.12", "pip.exe"}:
        return (sys.executable, "-m", "pip", *argv[1:])
    if executable in {"python", "python3", "python3.12", "python.exe"}:
        return (sys.executable, *argv[1:])
    return argv


def _text_from_timeout(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class LocalPytestExecutor:
    """Credential-free local executor used for deterministic baseline development/tests."""

    def execute(
        self,
        spec: WorkspaceSpec,
        *,
        execution_id: str,
    ) -> ExecutionResult:
        started = time.perf_counter()
        root = Path(spec.source_root).expanduser().resolve()
        working_directory = (root / spec.working_directory).resolve()

        try:
            working_directory.relative_to(root)
        except ValueError:
            return ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.INVALID,
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_detail="working_directory escapes source_root",
            )

        if not working_directory.is_dir():
            return ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.INVALID,
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_detail=f"working_directory does not exist: {working_directory}",
            )

        environment = dict(spec.environment)

        try:
            if spec.install_argv is not None:
                install = subprocess.run(
                    _normalize_python_command(spec.install_argv),
                    cwd=working_directory,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=spec.command_timeout_seconds,
                    check=False,
                )
                if install.returncode != 0:
                    return bound_execution_output(
                        spec,
                        ExecutionResult(
                            execution_id=execution_id,
                            outcome=ExecutionOutcome.INFRA_ERROR,
                            stdout=install.stdout,
                            stderr=install.stderr,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                            failure_detail=(
                                f"install command failed with exit code {install.returncode}"
                            ),
                        ),
                    )

            process = subprocess.run(
                _normalize_python_command(spec.pytest_argv),
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=spec.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.TIMEOUT,
                stdout=_text_from_timeout(exc.stdout),
                stderr=_text_from_timeout(exc.stderr),
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_detail=(
                    f"command exceeded timeout={spec.command_timeout_seconds}s"
                ),
            )
            return bound_execution_output(spec, result)
        except OSError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                outcome=ExecutionOutcome.INFRA_ERROR,
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_detail=str(exc),
            )

        result = ExecutionResult(
            execution_id=execution_id,
            outcome=execution_outcome_for_pytest_exit(process.returncode),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return bound_execution_output(spec, result)


def run_baseline(
    spec: WorkspaceSpec,
    executor: WorkspaceExecutor,
) -> BaselineResult:
    """Build sanitized evidence, execute the baseline, and stop on any non-ready result."""
    manifest = build_snapshot_manifest(spec)
    execution_id = f"baseline:{spec.workspace_id}"
    execution = executor.execute(spec, execution_id=execution_id)
    if execution.execution_id != execution_id:
        raise WorkspaceError(
            f"Executor returned execution_id={execution.execution_id!r}; "
            f"expected {execution_id!r}."
        )
    execution = bound_execution_output(spec, execution)
    post_execution_manifest = build_snapshot_manifest(spec)

    result = BaselineResult(
        workspace_id=spec.workspace_id,
        source_snapshot_id=spec.snapshot_id,
        manifest=manifest,
        manifest_sha256=manifest.manifest_sha256,
        post_execution_manifest_sha256=post_execution_manifest.manifest_sha256,
        execution=execution,
    )
    if not result.ready_for_mutation:
        raise BaselineNotReadyError(result)
    return result


def refund_workspace_spec(source_root: str = ".") -> WorkspaceSpec:
    """Canonical M0/M1 refund fixture configuration."""
    return WorkspaceSpec(
        workspace_id="refund-demo",
        source_root=source_root,
        snapshot_id="fixture:refund-v1",
        pytest_argv=(
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "examples/refund/test_refund.py",
        ),
        mutable_paths=("examples/refund/refund.py",),
        context_paths=("examples/refund/test_refund.py",),
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
    )
