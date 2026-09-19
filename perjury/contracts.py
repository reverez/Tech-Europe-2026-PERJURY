from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

CANONICAL_SNAPSHOT_EXCLUSIONS: tuple[str, ...] = (
    ".git",
    ".venv",
    ".env",
    ".perjury",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".eggs",
    "build",
    "dist",
    "node_modules",
    "*.egg-info",
    "*.pyc",
)

_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_?KEY|CREDENTIALS?)(?:$|_)",
    re.IGNORECASE,
)
_PYTHON_EXECUTABLE = re.compile(r"^python(?:3(?:\.12)?)?(?:\.exe)?$", re.IGNORECASE)


class ExecutionOutcome(StrEnum):
    PASS = "pass"
    TEST_FAIL = "test_fail"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


class MutationStatus(StrEnum):
    KILLED = "killed"
    SURVIVED = "survived"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


class NetworkPolicy(StrEnum):
    BLOCKED = "blocked"
    ENABLED = "enabled"


def execution_outcome_for_pytest_exit(code: int) -> ExecutionOutcome:
    """Map pytest's documented process exits into PERJURY semantics."""
    if code == 0:
        return ExecutionOutcome.PASS
    if code == 1:
        return ExecutionOutcome.TEST_FAIL
    if code in {2, 4, 5}:
        return ExecutionOutcome.INVALID
    return ExecutionOutcome.INFRA_ERROR


def mutation_status_for(outcome: ExecutionOutcome) -> MutationStatus:
    return {
        ExecutionOutcome.PASS: MutationStatus.SURVIVED,
        ExecutionOutcome.TEST_FAIL: MutationStatus.KILLED,
        ExecutionOutcome.INVALID: MutationStatus.INVALID,
        ExecutionOutcome.TIMEOUT: MutationStatus.TIMEOUT,
        ExecutionOutcome.INFRA_ERROR: MutationStatus.INFRA_ERROR,
    }[outcome]


def is_pytest_command(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    executable = PurePosixPath(argv[0]).name
    if executable in {"pytest", "pytest.exe"}:
        return True
    return (
        len(argv) >= 3
        and bool(_PYTHON_EXECUTABLE.fullmatch(executable))
        and argv[1:3] == ("-m", "pytest")
    )


def _validate_argv(argv: tuple[str, ...], *, require_pytest: bool = False) -> tuple[str, ...]:
    if not argv:
        raise ValueError("Command argv cannot be empty.")
    if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
        raise ValueError("Command argv entries must be non-empty strings without NUL bytes.")

    executable = PurePosixPath(argv[0]).name.lower()
    if executable in _SHELL_EXECUTABLES:
        raise ValueError("Shell executables are forbidden; commands must remain structured argv.")

    if require_pytest and not is_pytest_command(argv):
        raise ValueError("pytest_argv must invoke pytest directly or via python -m pytest.")
    return argv


def _validate_repo_relative_path(value: str, *, allow_dot: bool) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("Repository paths must be non-empty POSIX paths without NUL/backslashes.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe repository-relative path: {value!r}")
    normalized = str(path)
    if normalized == "." and not allow_dot:
        raise ValueError("A concrete repository-relative path is required.")
    return normalized


def _paths_overlap(left: str, right: str) -> bool:
    left_path = PurePosixPath(left)
    right_path = PurePosixPath(right)
    return (
        left_path == right_path
        or left_path in right_path.parents
        or right_path in left_path.parents
    )


class WorkspaceSpec(BaseModel):
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    source_root: str = Field(min_length=1, max_length=4096)
    snapshot_id: str = Field(min_length=1, max_length=160)
    python_version: Literal["3.12"] = "3.12"
    install_argv: tuple[str, ...] | None = None
    pytest_argv: tuple[str, ...] = ("python", "-m", "pytest", "-q")
    working_directory: str = "."
    command_timeout_seconds: int = Field(default=120, ge=1, le=300)
    mutation_concurrency: int = Field(default=8, ge=1, le=10)
    mutable_paths: tuple[str, ...] = Field(min_length=1)
    context_paths: tuple[str, ...] = Field(min_length=1)
    exclusion_patterns: tuple[str, ...] = CANONICAL_SNAPSHOT_EXCLUSIONS
    max_output_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    max_snapshot_files: int = Field(default=512, ge=1, le=5_000)
    max_snapshot_bytes: int = Field(default=10_485_760, ge=1_024, le=104_857_600)
    network_policy: NetworkPolicy = NetworkPolicy.BLOCKED
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("source_root")
    @classmethod
    def source_root_must_be_plain_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("source_root cannot contain NUL bytes.")
        return value

    @field_validator("working_directory")
    @classmethod
    def working_directory_must_be_relative(cls, value: str) -> str:
        return _validate_repo_relative_path(value, allow_dot=True)

    @field_validator("mutable_paths", "context_paths")
    @classmethod
    def allowlists_must_be_safe_relative_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_repo_relative_path(value, allow_dot=False) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Workspace path allowlists cannot contain duplicates.")
        return normalized

    @field_validator("exclusion_patterns")
    @classmethod
    def exclusions_must_include_canonical_policy(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value or "\x00" in value for value in values):
            raise ValueError("Snapshot exclusion patterns must be non-empty and NUL-free.")
        missing = set(CANONICAL_SNAPSHOT_EXCLUSIONS).difference(values)
        if missing:
            raise ValueError(
                "Snapshot exclusions cannot remove canonical safety entries: "
                + ", ".join(sorted(missing))
            )
        return values

    @field_validator("install_argv")
    @classmethod
    def install_command_must_be_structured(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _validate_argv(value)

    @field_validator("pytest_argv")
    @classmethod
    def pytest_command_must_be_structured(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, require_pytest=True)

    @field_validator("environment")
    @classmethod
    def environment_must_be_explicit_and_non_secret(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("Workspace environment is limited to 32 explicit variables.")
        cleaned: dict[str, str] = {}
        for name, env_value in value.items():
            if not _ENV_NAME.fullmatch(name):
                raise ValueError(f"Invalid environment variable name: {name!r}")
            if _SECRET_ENV_NAME.search(name):
                raise ValueError(f"Secret-like environment variable is forbidden: {name!r}")
            if "\x00" in env_value or len(env_value) > 4096:
                raise ValueError(
                    f"Environment value for {name!r} must be NUL-free and at most 4096 chars."
                )
            cleaned[name] = env_value
        return cleaned

    @model_validator(mode="after")
    def mutable_and_context_paths_must_be_separate(self) -> WorkspaceSpec:
        overlaps = [
            (mutable, context)
            for mutable in self.mutable_paths
            for context in self.context_paths
            if _paths_overlap(mutable, context)
        ]
        if overlaps:
            mutable, context = overlaps[0]
            raise ValueError(
                "Mutable implementation paths must not overlap context/test paths: "
                f"{mutable!r} vs {context!r}."
            )
        return self


class SnapshotFile(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SnapshotManifest(BaseModel):
    workspace_id: str
    source_snapshot_id: str
    manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    files: tuple[SnapshotFile, ...]
    total_bytes: int = Field(ge=0)

    @computed_field
    @property
    def file_count(self) -> int:
        return len(self.files)

    @model_validator(mode="after")
    def totals_must_match_files(self) -> SnapshotManifest:
        expected = sum(file.size_bytes for file in self.files)
        if self.total_bytes != expected:
            raise ValueError(
                f"Snapshot total_bytes={self.total_bytes} does not match file sum={expected}."
            )
        return self


class MutationProposal(BaseModel):
    id: str = Field(pattern=r"^M\d{2,}$")
    file_path: str
    description: str
    hypothesis: str
    original_snippet: str
    mutated_snippet: str


class MutationBatch(BaseModel):
    rationale: str
    mutations: list[MutationProposal] = Field(min_length=1, max_length=20)


class AppliedMutation(BaseModel):
    mutation_id: str = Field(pattern=r"^M\d{2,}$")
    base_snapshot_id: str
    base_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_path: str
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutated_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutated_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diff: str = Field(min_length=1)

    @model_validator(mode="after")
    def mutation_evidence_must_show_a_real_change(self) -> AppliedMutation:
        if self.original_sha256 == self.mutated_sha256:
            raise ValueError("Applied mutation must change the target file content hash.")
        if self.base_manifest_sha256 == self.mutated_manifest_sha256:
            raise ValueError("Applied mutation must change the workspace manifest identity.")
        return self


class ExecutionResult(BaseModel):
    execution_id: str
    outcome: ExecutionOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    failure_detail: str | None = None
    cleanup_error: str | None = None

    @property
    def mutation_status(self) -> MutationStatus:
        """Project generic execution outcome into mutation-testing terminology."""
        return mutation_status_for(self.outcome)

    @model_validator(mode="after")
    def outcome_must_match_known_pytest_exit(self) -> ExecutionResult:
        if self.exit_code is not None:
            expected = execution_outcome_for_pytest_exit(self.exit_code)
            if self.outcome is not expected:
                raise ValueError(
                    f"Exit code {self.exit_code} requires outcome {expected.value!r}, "
                    f"not {self.outcome.value!r}."
                )
        elif self.outcome in {ExecutionOutcome.PASS, ExecutionOutcome.TEST_FAIL}:
            raise ValueError(f"{self.outcome.value!r} requires a concrete pytest exit code.")
        return self


class BaselineResult(BaseModel):
    workspace_id: str
    source_snapshot_id: str
    manifest: SnapshotManifest
    manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    post_execution_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    execution: ExecutionResult

    @computed_field
    @property
    def ready_for_mutation(self) -> bool:
        return (
            self.execution.outcome is ExecutionOutcome.PASS
            and self.execution.cleanup_error is None
            and self.manifest_sha256 == self.post_execution_manifest_sha256
        )

    @model_validator(mode="after")
    def execution_identity_must_match_workspace(self) -> BaselineResult:
        if self.manifest.workspace_id != self.workspace_id:
            raise ValueError("Baseline manifest workspace_id must match BaselineResult.")
        if self.manifest.source_snapshot_id != self.source_snapshot_id:
            raise ValueError("Baseline manifest snapshot identity must match BaselineResult.")
        if self.manifest.manifest_sha256 != self.manifest_sha256:
            raise ValueError("Baseline manifest hash must match retained manifest evidence.")

        expected = f"baseline:{self.workspace_id}"
        if self.execution.execution_id != expected:
            raise ValueError(
                f"Baseline execution_id must be {expected!r}, "
                f"not {self.execution.execution_id!r}."
            )
        return self


class SurvivorAnalysis(BaseModel):
    mutation_id: str
    behavioural_gap: str
    test_intent: str
    possibly_equivalent: bool = False
    reasoning: str


class TestProposal(BaseModel):
    mutation_id: str
    test_name: str
    target_file: str
    test_code: str
    explanation: str


class VerificationEvidence(BaseModel):
    mutation_id: str
    original_outcome: ExecutionOutcome
    mutant_outcome: ExecutionOutcome
    original_exit_code: int | None = None
    mutant_exit_code: int | None = None
    original_stdout: str = ""
    mutant_stdout: str = ""
    original_duration_ms: int = Field(ge=0)
    mutant_duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def outcomes_must_match_known_pytest_exits(self) -> VerificationEvidence:
        pairs = (
            ("original", self.original_outcome, self.original_exit_code),
            ("mutant", self.mutant_outcome, self.mutant_exit_code),
        )
        for label, outcome, exit_code in pairs:
            if exit_code is not None:
                expected = execution_outcome_for_pytest_exit(exit_code)
                if outcome is not expected:
                    raise ValueError(
                        f"{label} exit code {exit_code} requires outcome "
                        f"{expected.value!r}, not {outcome.value!r}."
                    )
            elif outcome in {ExecutionOutcome.PASS, ExecutionOutcome.TEST_FAIL}:
                raise ValueError(
                    f"{label} {outcome.value!r} requires a concrete pytest exit code."
                )
        return self

    @property
    def verified(self) -> bool:
        return (
            self.original_outcome is ExecutionOutcome.PASS
            and self.mutant_outcome is ExecutionOutcome.TEST_FAIL
        )


class HardeningResult(BaseModel):
    mutation_id: str
    verdict: Literal["verified", "rejected", "inconclusive"]
    evidence: VerificationEvidence
    explanation: str

    @model_validator(mode="after")
    def verdict_must_match_evidence(self) -> HardeningResult:
        if self.mutation_id != self.evidence.mutation_id:
            raise ValueError(
                "Hardening result mutation_id must match verification evidence."
            )

        if self.evidence.verified:
            expected = "verified"
        elif self.evidence.original_outcome is ExecutionOutcome.TEST_FAIL:
            expected = "rejected"
        else:
            expected = "inconclusive"

        if self.verdict != expected:
            raise ValueError(
                f"Verdict {self.verdict!r} contradicts semantic verification "
                f"evidence; expected {expected!r}."
            )
        return self
