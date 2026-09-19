from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import modal
import pytest

from perjury.contracts import ExecutionOutcome, NetworkPolicy, WorkspaceSpec
from perjury.modal_runner import ModalWorkspaceExecutor
from perjury.workspace import (
    LocalPytestExecutor,
    build_snapshot_manifest,
    run_baseline,
)


class FakeStream:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> str:
        return self.text


class FakeProcess:
    def __init__(self, returncode: int, *, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)

    def wait(self) -> int:
        return self.returncode


@dataclass
class FakeFilesystem:
    files: dict[str, bytes] = field(default_factory=dict)
    directories: list[str] = field(default_factory=list)
    write_error: Exception | None = None

    def make_directory(self, remote_path: str, *, create_parents: bool = True) -> None:
        self.directories.append(remote_path)

    def write_bytes(self, data: bytes, remote_path: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.files[remote_path] = data


@dataclass
class FakeSandbox:
    processes: list[FakeProcess | Exception]
    filesystem: FakeFilesystem = field(default_factory=FakeFilesystem)
    cleanup_error: Exception | None = None
    exec_calls: list[tuple[tuple[str, ...], dict[str, object]]] = field(default_factory=list)
    terminate_calls: list[bool] = field(default_factory=list)

    def exec(self, *args: str, **kwargs: object) -> FakeProcess:
        self.exec_calls.append((args, kwargs))
        planned = self.processes.pop(0)
        if isinstance(planned, Exception):
            raise planned
        return planned

    def terminate(self, *, wait: bool = False) -> int | None:
        self.terminate_calls.append(wait)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return 0 if wait else None


@dataclass
class FakeSandboxFactory:
    sandbox: FakeSandbox
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, **kwargs: object) -> FakeSandbox:
        self.calls.append(kwargs)
        return self.sandbox


def write_workspace(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "asset.bin").write_bytes(b"\x00\xff\x10")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from src.app import VALUE\n\n\ndef test_value() -> None:\n    assert VALUE == 1\n",
        encoding="utf-8",
    )


def workspace_spec(root: Path, **updates: object) -> WorkspaceSpec:
    values: dict[str, object] = {
        "workspace_id": "modal-unit",
        "source_root": str(root),
        "snapshot_id": "fixture:modal-v1",
        "pytest_argv": ("python", "-m", "pytest", "-q", "tests/test_app.py"),
        "mutable_paths": ("src",),
        "context_paths": ("tests",),
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
    }
    values.update(updates)
    return WorkspaceSpec(**values)


def executor_for(
    sandbox: FakeSandbox,
) -> tuple[ModalWorkspaceExecutor, FakeSandboxFactory, list[str]]:
    provider_calls: list[str] = []
    factory = FakeSandboxFactory(sandbox)

    def app_factory() -> object:
        provider_calls.append("app")
        return object()

    def image_factory() -> object:
        provider_calls.append("image")
        return object()

    executor = ModalWorkspaceExecutor(
        app_factory=app_factory,
        image_factory=image_factory,
        sandbox_factory=factory,
    )
    return executor, factory, provider_calls


def test_modal_executor_uploads_only_exact_sanitized_manifest(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    (tmp_path / ".env").write_text("GOOGLE_API_KEY=must-not-upload\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "secret").write_text("private\n", encoding="utf-8")
    (tmp_path / ".perjury").mkdir()
    (tmp_path / ".perjury" / "evidence.json").write_text("{}\n", encoding="utf-8")
    spec = workspace_spec(tmp_path)
    manifest = build_snapshot_manifest(spec)
    sandbox = FakeSandbox([FakeProcess(0, stdout="3 passed\n")])
    executor, factory, provider_calls = executor_for(sandbox)

    result = executor.execute(spec, execution_id="baseline:modal-unit", manifest=manifest)

    assert result.outcome is ExecutionOutcome.PASS
    assert result.exit_code == 0
    assert set(sandbox.filesystem.files) == {
        f"/workspace/{entry.path}" for entry in manifest.files
    }
    assert sandbox.filesystem.files["/workspace/src/asset.bin"] == b"\x00\xff\x10"
    assert not any(".env" in path for path in sandbox.filesystem.files)
    assert not any(".git" in path for path in sandbox.filesystem.files)
    assert not any(".venv" in path for path in sandbox.filesystem.files)
    assert not any(".perjury" in path for path in sandbox.filesystem.files)
    assert provider_calls == ["app", "image"]
    assert factory.calls[0]["block_network"] is True
    assert factory.calls[0]["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert factory.calls[0]["secrets"] == ()
    assert factory.calls[0]["include_oidc_identity_token"] is False
    assert sandbox.exec_calls == [
        (
            spec.pytest_argv,
            {
                "workdir": "/workspace",
                "timeout": spec.command_timeout_seconds,
                "env": {"PYTHONDONTWRITEBYTECODE": "1"},
                "secrets": (),
            },
        )
    ]
    assert sandbox.terminate_calls == [True]


def test_network_access_requires_explicit_typed_opt_in(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path, network_policy=NetworkPolicy.ENABLED)
    sandbox = FakeSandbox([FakeProcess(0)])
    executor, factory, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="network-opt-in")

    assert result.outcome is ExecutionOutcome.PASS
    assert spec.model_dump(mode="json")["network_policy"] == "enabled"
    assert factory.calls[0]["block_network"] is False


def test_install_failure_is_infrastructure_error_and_stops_before_pytest(
    tmp_path: Path,
) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(
        tmp_path,
        install_argv=("python", "-m", "pip", "install", "-e", "."),
    )
    sandbox = FakeSandbox([FakeProcess(7, stderr="unsupported layout\n")])
    executor, _, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="install-failure")

    assert result.outcome is ExecutionOutcome.INFRA_ERROR
    assert result.exit_code is None
    assert result.stderr == "unsupported layout\n"
    assert result.failure_detail == "install command failed with exit code 7"
    assert len(sandbox.exec_calls) == 1
    assert sandbox.terminate_calls == [True]


@pytest.mark.parametrize(
    ("planned", "expected", "exit_code"),
    [
        (FakeProcess(0), ExecutionOutcome.PASS, 0),
        (FakeProcess(1), ExecutionOutcome.TEST_FAIL, 1),
        (FakeProcess(4), ExecutionOutcome.INVALID, 4),
        (modal.exception.ExecTimeoutError("too slow"), ExecutionOutcome.TIMEOUT, None),
        (RuntimeError("provider exploded"), ExecutionOutcome.INFRA_ERROR, None),
    ],
)
def test_cleanup_runs_for_every_terminal_execution_path(
    tmp_path: Path,
    planned: FakeProcess | Exception,
    expected: ExecutionOutcome,
    exit_code: int | None,
) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    sandbox = FakeSandbox([planned])
    executor, _, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="terminal-path")

    assert result.outcome is expected
    assert result.exit_code == exit_code
    assert sandbox.terminate_calls == [True]


def test_cleanup_error_is_retained_without_erasing_primary_outcome(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    sandbox = FakeSandbox(
        [FakeProcess(1, stdout="failed\n")],
        cleanup_error=RuntimeError("terminate failed"),
    )
    executor, _, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="cleanup-failure")

    assert result.outcome is ExecutionOutcome.TEST_FAIL
    assert result.exit_code == 1
    assert result.cleanup_error == "Sandbox cleanup failed: terminate failed"


def test_provider_error_during_upload_still_cleans_up(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    sandbox = FakeSandbox(
        [FakeProcess(0)],
        filesystem=FakeFilesystem(write_error=RuntimeError("upload failed")),
    )
    executor, _, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="upload-failure")

    assert result.outcome is ExecutionOutcome.INFRA_ERROR
    assert result.failure_detail == "Sandbox execution failure: upload failed"
    assert sandbox.terminate_calls == [True]


def test_modal_output_is_bounded_with_truncation_metadata(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path, max_output_bytes=1_024)
    sandbox = FakeSandbox(
        [FakeProcess(0, stdout="x" * 2_000, stderr="y" * 2_000)]
    )
    executor, _, _ = executor_for(sandbox)

    result = executor.execute(spec, execution_id="bounded-output")

    assert len(result.stdout.encode("utf-8")) == 1_024
    assert len(result.stderr.encode("utf-8")) == 1_024
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_stale_manifest_is_rejected_before_modal_initialization(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    manifest = build_snapshot_manifest(spec)
    (tmp_path / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    sandbox = FakeSandbox([FakeProcess(0)])
    executor, factory, provider_calls = executor_for(sandbox)

    result = executor.execute(spec, execution_id="stale", manifest=manifest)

    assert result.outcome is ExecutionOutcome.INVALID
    assert "does not match the manifest" in (result.failure_detail or "")
    assert factory.calls == []
    assert provider_calls == []
    assert sandbox.terminate_calls == []


def test_local_and_modal_baseline_share_snapshot_and_semantic_contract(
    tmp_path: Path,
) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    local = run_baseline(spec, LocalPytestExecutor())
    sandbox = FakeSandbox([FakeProcess(0, stdout="1 passed\n")])
    executor, _, _ = executor_for(sandbox)

    remote = run_baseline(spec, executor)

    assert local.manifest.model_dump(mode="json") == remote.manifest.model_dump(mode="json")
    assert local.execution.outcome is ExecutionOutcome.PASS
    assert remote.execution.outcome is ExecutionOutcome.PASS
    assert remote.ready_for_mutation is True


def test_executor_construction_does_not_initialize_modal(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec = workspace_spec(tmp_path)
    manifest = build_snapshot_manifest(spec)
    (tmp_path / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    calls: list[str] = []

    def unexpected_factory() -> object:
        calls.append("called")
        return object()

    executor = ModalWorkspaceExecutor(
        app_factory=unexpected_factory,
        image_factory=unexpected_factory,
        sandbox_factory=lambda **kwargs: pytest.fail("sandbox must not be created"),
    )
    assert calls == []

    result = executor.execute(spec, execution_id="invalid-preflight", manifest=manifest)

    assert result.outcome is ExecutionOutcome.INVALID
    assert calls == []
