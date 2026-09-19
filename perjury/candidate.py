"""Safe materialization of a generated pytest candidate (#13).

The model proposes a ``TestProposal``; it never receives write authority. PERJURY validates
``target_file`` against the WorkspaceSpec test/context allowlist and the baseline manifest,
derives a deterministic collision-safe *sibling* filename, and creates it with create-new
semantics inside an isolated workspace only. The source repository is never written to.

Preflight: syntax (``ast``) and a pytest ``--collect-only`` run that explicitly names the
candidate. The collection run goes through the caller's ``WorkspaceExecutor`` (Modal in live use),
so model-generated code is never imported on the host by this module.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import os
import shutil
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, model_validator

from .contracts import (
    BaselineResult,
    ExecutionOutcome,
    ExecutionResult,
    TestProposal,
    WorkspaceSpec,
    is_pytest_command,
)
from .mutation import materialize_baseline_workspace
from .workspace import WorkspaceError, WorkspaceExecutor, _is_excluded, build_snapshot_manifest

MAX_CANDIDATE_BYTES = 65_536
MAX_COLLISION_SUFFIX = 99


class CandidateReason(StrEnum):
    UNSAFE_PATH = "unsafe_path"
    FORBIDDEN_TARGET = "forbidden_target"
    TARGET_MISSING = "target_missing"
    EMPTY_CODE = "empty_code"
    CODE_TOO_LARGE = "code_too_large"
    INVALID_SYNTAX = "invalid_syntax"
    COLLISION_EXHAUSTED = "collision_exhausted"
    ALREADY_EXISTS = "already_exists"
    COLLECTION_FAILED = "collection_failed"
    PREFLIGHT_UNAVAILABLE = "preflight_unavailable"


class CandidateRejected(WorkspaceError):
    """The candidate cannot be used. ``outcome`` is INVALID for anything wrong with the
    candidate itself, and TIMEOUT/INFRA_ERROR when the preflight could not run to a verdict."""

    def __init__(
        self,
        reason: CandidateReason,
        detail: str,
        *,
        outcome: ExecutionOutcome = ExecutionOutcome.INVALID,
        execution: ExecutionResult | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.outcome = outcome
        self.execution = execution
        super().__init__(f"[{reason.value}] {detail}")


class CandidatePlan(BaseModel):
    """A validated candidate, not yet written anywhere."""

    mutation_id: str
    target_file: str
    candidate_path: str
    test_name: str
    code: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    diff: str = Field(min_length=1)
    base_snapshot_id: str
    base_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_must_describe_the_code(self) -> CandidatePlan:
        data = self.code.encode("utf-8")
        if hashlib.sha256(data).hexdigest() != self.sha256 or len(data) != self.size_bytes:
            raise ValueError("Candidate hash/size does not match its code.")
        if PurePosixPath(self.candidate_path).parent != PurePosixPath(self.target_file).parent:
            raise ValueError("Candidate must be a sibling of its target test file.")
        return self


class CandidateEvidence(BaseModel):
    """Retained run evidence: code, path, diff and the collection preflight result."""

    plan: CandidatePlan
    syntax_ok: bool
    collection: ExecutionResult


def _within(path: str, allowlist: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate == PurePosixPath(a) or PurePosixPath(a) in candidate.parents for a in allowlist
    )


def _normalize(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw:
        raise CandidateRejected(
            CandidateReason.UNSAFE_PATH, "target_file is empty or has NUL/backslash"
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise CandidateRejected(
            CandidateReason.UNSAFE_PATH, "target_file must be repository-relative, no traversal"
        )
    return str(path)


def plan_candidate(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    proposal: TestProposal,
) -> CandidatePlan:
    """Validate a TestProposal and derive its deterministic sibling candidate path."""
    target = _normalize(proposal.target_file)
    if _is_excluded(target, spec.exclusion_patterns):
        raise CandidateRejected(CandidateReason.FORBIDDEN_TARGET, "target is in an excluded path")
    if _within(target, spec.mutable_paths):
        raise CandidateRejected(
            CandidateReason.FORBIDDEN_TARGET, "target is an implementation file, not a test"
        )
    if not _within(target, spec.context_paths):
        raise CandidateRejected(
            CandidateReason.FORBIDDEN_TARGET, "target is outside the allowed test/context paths"
        )
    known = {f.path for f in baseline.manifest.files}
    if target not in known:
        raise CandidateRejected(
            CandidateReason.TARGET_MISSING, "target_file does not exist in the baseline snapshot"
        )
    if not target.endswith(".py"):
        raise CandidateRejected(CandidateReason.FORBIDDEN_TARGET, "target must be a Python file")

    parent = PurePosixPath(target).parent
    for suffix in range(1, MAX_COLLISION_SUFFIX + 1):
        stem = f"test_perjury_{proposal.mutation_id}" + ("" if suffix == 1 else f"_{suffix}")
        candidate = str(parent / f"{stem}.py")
        if candidate not in known:
            break
    else:
        raise CandidateRejected(
            CandidateReason.COLLISION_EXHAUSTED,
            "no free candidate filename in the target directory",
        )
    if _is_excluded(candidate, spec.exclusion_patterns) or _within(candidate, spec.mutable_paths):
        raise CandidateRejected(
            CandidateReason.FORBIDDEN_TARGET, "candidate directory is not an allowed test location"
        )

    code = proposal.test_code
    if not code.strip():
        raise CandidateRejected(CandidateReason.EMPTY_CODE, "test_code is empty")
    if "\x00" in code:
        raise CandidateRejected(CandidateReason.INVALID_SYNTAX, "test_code contains NUL bytes")
    if not code.endswith("\n"):
        code += "\n"
    data = code.encode("utf-8")
    if len(data) > MAX_CANDIDATE_BYTES:
        raise CandidateRejected(
            CandidateReason.CODE_TOO_LARGE, f"test_code exceeds {MAX_CANDIDATE_BYTES} bytes"
        )
    try:
        ast.parse(code, filename=candidate)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise CandidateRejected(
            CandidateReason.INVALID_SYNTAX, f"{type(exc).__name__}: {exc}"
        ) from exc

    diff = "".join(
        difflib.unified_diff(
            [], code.splitlines(keepends=True), fromfile="/dev/null", tofile=f"b/{candidate}"
        )
    )
    return CandidatePlan(
        mutation_id=proposal.mutation_id,
        target_file=target,
        candidate_path=candidate,
        test_name=proposal.test_name,
        code=code,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        diff=diff,
        base_snapshot_id=baseline.source_snapshot_id,
        base_manifest_sha256=baseline.manifest_sha256,
    )


def write_candidate(root: Path, plan: CandidatePlan) -> Path:
    """Create the candidate inside an isolated ``root`` with create-new semantics.

    Never creates directories, follows symlinks, overwrites or appends.
    """
    root = root.resolve()
    current = root
    parts = PurePosixPath(plan.candidate_path).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise CandidateRejected(
                CandidateReason.UNSAFE_PATH,
                f"candidate directory is missing or a symlink: {part!r}",
            )
    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise CandidateRejected(
            CandidateReason.UNSAFE_PATH, "candidate directory escapes the isolated workspace"
        ) from exc

    target = current / parts[-1]
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError as exc:
        raise CandidateRejected(
            CandidateReason.ALREADY_EXISTS, f"refusing to overwrite {plan.candidate_path!r}"
        ) from exc
    except OSError as exc:
        raise CandidateRejected(
            CandidateReason.UNSAFE_PATH, f"cannot create candidate safely: {exc}"
        ) from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(plan.code.encode("utf-8"))
    return target


def spec_with_candidate(
    spec: WorkspaceSpec,
    root: Path,
    plan: CandidatePlan,
    *,
    snapshot_id: str,
    pytest_argv: tuple[str, ...] | None = None,
) -> WorkspaceSpec:
    """World spec rooted at ``root`` whose pytest command explicitly names the candidate."""
    argv = pytest_argv if pytest_argv is not None else (*spec.pytest_argv, plan.candidate_path)
    return spec.model_copy(
        update={
            "source_root": str(root),
            "snapshot_id": snapshot_id,
            "pytest_argv": argv,
            "context_paths": (*spec.context_paths, plan.candidate_path),
        }
    )


def _collect_only_argv(spec: WorkspaceSpec, plan: CandidatePlan) -> tuple[str, ...]:
    if not is_pytest_command(spec.pytest_argv):
        raise CandidateRejected(
            CandidateReason.PREFLIGHT_UNAVAILABLE, "pytest_argv is not a pytest command"
        )
    prefix = (
        spec.pytest_argv[:1] if spec.pytest_argv[0].endswith("pytest") else spec.pytest_argv[:3]
    )
    return (*prefix, "-q", "-p", "no:cacheprovider", "--collect-only", plan.candidate_path)


def preflight_candidate(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: CandidatePlan,
    executor: WorkspaceExecutor,
) -> CandidateEvidence:
    """Materialize into a throwaway workspace and require pytest to collect the candidate."""
    if (
        plan.base_manifest_sha256 != baseline.manifest_sha256
        or plan.base_snapshot_id != baseline.source_snapshot_id
    ):
        raise CandidateRejected(
            CandidateReason.PREFLIGHT_UNAVAILABLE,
            "candidate was planned against a different baseline",
        )
    root = materialize_baseline_workspace(spec, baseline)
    try:
        write_candidate(root, plan)
        digest = plan.sha256[:16]
        collect_spec = spec_with_candidate(
            spec,
            root,
            plan,
            snapshot_id=f"candidate-preflight:{plan.mutation_id}:{digest}",
            pytest_argv=_collect_only_argv(spec, plan),
        )
        manifest = build_snapshot_manifest(collect_spec)
        execution = executor.execute(
            collect_spec,
            execution_id=f"candidate-preflight:{plan.mutation_id}",
            manifest=manifest,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if execution.outcome is ExecutionOutcome.PASS:
        return CandidateEvidence(plan=plan, syntax_ok=True, collection=execution)
    if execution.outcome in {ExecutionOutcome.TIMEOUT, ExecutionOutcome.INFRA_ERROR}:
        raise CandidateRejected(
            CandidateReason.PREFLIGHT_UNAVAILABLE,
            f"collection preflight did not complete: {execution.outcome.value}",
            outcome=execution.outcome,
            execution=execution,
        )
    raise CandidateRejected(
        CandidateReason.COLLECTION_FAILED,
        f"pytest could not collect the candidate (outcome={execution.outcome.value})",
        execution=execution,
    )
