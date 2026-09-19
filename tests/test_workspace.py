from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from perjury.contracts import (
    CANONICAL_SNAPSHOT_EXCLUSIONS,
    ExecutionOutcome,
    ExecutionResult,
    NetworkPolicy,
    WorkspaceSpec,
)
from perjury.workspace import (
    BaselineNotReadyError,
    LocalPytestExecutor,
    SnapshotPolicyError,
    build_snapshot_manifest,
    refund_workspace_spec,
    run_baseline,
)


def write_minimal_workspace(root: Path) -> None:
    (root / "app.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value() -> None:\n    assert value() == 1\n",
        encoding="utf-8",
    )


def minimal_spec(root: Path, **updates: object) -> WorkspaceSpec:
    values: dict[str, object] = {
        "workspace_id": "unit-workspace",
        "source_root": str(root),
        "snapshot_id": "fixture:unit-v1",
        "pytest_argv": ("python", "-m", "pytest", "-q", "test_app.py"),
        "mutable_paths": ("app.py",),
        "context_paths": ("test_app.py",),
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
    }
    values.update(updates)
    return WorkspaceSpec(**values)


@dataclass
class FakeExecutor:
    outcome: ExecutionOutcome
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    cleanup_error: str | None = None

    def execute(
        self,
        spec: WorkspaceSpec,
        *,
        execution_id: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            execution_id=execution_id,
            outcome=self.outcome,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_ms=1,
            cleanup_error=self.cleanup_error,
        )


def test_workspace_spec_serializes_safety_policy(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    spec = minimal_spec(tmp_path)

    payload = spec.model_dump(mode="json")

    assert payload["network_policy"] == NetworkPolicy.BLOCKED
    assert payload["python_version"] == "3.12"
    assert payload["mutation_concurrency"] == 8
    assert payload["environment"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert set(CANONICAL_SNAPSHOT_EXCLUSIONS).issubset(payload["exclusion_patterns"])


def test_workspace_spec_rejects_overlapping_mutable_and_context_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        minimal_spec(
            tmp_path,
            mutable_paths=("src",),
            context_paths=("src/test_feature.py",),
        )


def test_workspace_spec_rejects_shell_pytest_command(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        minimal_spec(
            tmp_path,
            pytest_argv=("bash", "-lc", "pytest -q"),
        )


def test_workspace_spec_rejects_secret_like_environment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        minimal_spec(
            tmp_path,
            environment={"GOOGLE_API_KEY": "should-never-be-forwarded"},
        )


def test_workspace_spec_cannot_remove_canonical_exclusions(tmp_path: Path) -> None:
    weakened = tuple(
        pattern for pattern in CANONICAL_SNAPSHOT_EXCLUSIONS if pattern != ".env"
    )
    with pytest.raises(ValidationError):
        minimal_spec(tmp_path, exclusion_patterns=weakened)


def test_snapshot_manifest_excludes_secret_and_runtime_paths(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=yes", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "python").write_text("binary-ish", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.bin").write_bytes(b"artifact")

    spec = minimal_spec(tmp_path)
    first = build_snapshot_manifest(spec)
    second = build_snapshot_manifest(spec)
    paths = {file.path for file in first.files}

    assert {"app.py", "test_app.py", "safe.txt"}.issubset(paths)
    assert ".env" not in paths
    assert not any(path.startswith(".git/") for path in paths)
    assert not any(path.startswith(".venv/") for path in paths)
    assert not any(path.startswith("build/") for path in paths)
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.total_bytes == sum(file.size_bytes for file in first.files)


def test_snapshot_file_limit_fails_before_execution(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    (tmp_path / "extra.py").write_text("x = 1\n", encoding="utf-8")
    spec = minimal_spec(tmp_path, max_snapshot_files=2)

    with pytest.raises(SnapshotPolicyError, match="max_snapshot_files"):
        build_snapshot_manifest(spec)


def test_snapshot_byte_limit_fails_before_execution(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    (tmp_path / "large.bin").write_bytes(b"x" * 2_000)
    spec = minimal_spec(tmp_path, max_snapshot_bytes=1_024)

    with pytest.raises(SnapshotPolicyError, match="max_snapshot_bytes"):
        build_snapshot_manifest(spec)


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [
        (ExecutionOutcome.TEST_FAIL, 1),
        (ExecutionOutcome.INVALID, 5),
        (ExecutionOutcome.TIMEOUT, None),
        (ExecutionOutcome.INFRA_ERROR, None),
    ],
)
def test_non_pass_baseline_fails_fast_with_evidence(
    tmp_path: Path,
    outcome: ExecutionOutcome,
    exit_code: int | None,
) -> None:
    write_minimal_workspace(tmp_path)
    spec = minimal_spec(tmp_path)

    with pytest.raises(BaselineNotReadyError) as caught:
        run_baseline(spec, FakeExecutor(outcome=outcome, exit_code=exit_code))

    assert caught.value.result.execution.outcome is outcome
    assert caught.value.result.ready_for_mutation is False


def test_cleanup_error_blocks_mutation_without_rewriting_primary_pass(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    spec = minimal_spec(tmp_path)

    with pytest.raises(BaselineNotReadyError) as caught:
        run_baseline(
            spec,
            FakeExecutor(
                outcome=ExecutionOutcome.PASS,
                exit_code=0,
                cleanup_error="sandbox teardown failed",
            ),
        )

    assert caught.value.result.execution.outcome is ExecutionOutcome.PASS
    assert caught.value.result.execution.cleanup_error == "sandbox teardown failed"


def test_baseline_output_is_utf8_safely_bounded(tmp_path: Path) -> None:
    write_minimal_workspace(tmp_path)
    spec = minimal_spec(tmp_path, max_output_bytes=1_024)
    noisy = "é" * 1_000

    result = run_baseline(
        spec,
        FakeExecutor(
            outcome=ExecutionOutcome.PASS,
            exit_code=0,
            stdout=noisy,
            stderr=noisy,
        ),
    )

    assert len(result.execution.stdout.encode("utf-8")) <= 1_024
    assert len(result.execution.stderr.encode("utf-8")) <= 1_024
    assert result.execution.stdout_truncated is True
    assert result.execution.stderr_truncated is True
    assert result.ready_for_mutation is True


def test_refund_fixture_baseline_passes_through_local_executor() -> None:
    result = run_baseline(refund_workspace_spec(), LocalPytestExecutor())

    assert result.execution.outcome is ExecutionOutcome.PASS
    assert result.execution.exit_code == 0
    assert result.ready_for_mutation is True
    assert result.manifest_sha256.startswith("sha256:")
