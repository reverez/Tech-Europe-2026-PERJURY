"""Mutation planning, validation, deduplication and bounded replenishment (#10).

The model boundary is the injectable ``MutationPlanner`` protocol, so deterministic tests never
need credentials or network. Every proposal, live or injected, passes the same Pydantic contract
and the same deterministic gate before it can count toward the 6-mutation minimum:

  shape -> path policy (implementation-only) -> no-op -> exact single anchor -> deduplication
  -> applicator on a disposable copy -> Python compile preflight

Accepted mutations receive stable IDs (M01, M02, ... in acceptance order), so refill proposals
that reuse a model-chosen ID cannot collide. At most one replenishment request is made.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError, model_validator

from .context import ContextBudget, pack_context, render_mutation_request
from .contracts import (
    AppliedMutation,
    BaselineResult,
    MutationBatch,
    MutationProposal,
    WorkspaceSpec,
)
from .mutation import (
    MutationApplicationError,
    StaleSnapshotError,
    UnsafeMutationPathError,
    UnsupportedMutationTargetError,
    apply_mutation,
)
from .workspace import WorkspaceError

MIN_MUTATIONS = 6
MAX_MUTATIONS = 10
DEFAULT_TARGET_MUTATIONS = 8


class PlanningFailureCode(StrEnum):
    INSUFFICIENT_VALID_MUTATIONS = "insufficient_valid_mutations"
    PROVIDER_ERROR = "provider_error"
    STALE_BASELINE = "stale_baseline"


class RejectionReason(StrEnum):
    INVALID_OUTPUT = "invalid_output"
    DUPLICATE_ID = "duplicate_id"
    UNSAFE_PATH = "unsafe_path"
    FORBIDDEN_TARGET = "forbidden_target"
    UNSUPPORTED_TARGET = "unsupported_target"
    NO_OP = "no_op"
    MISSING_ANCHOR = "missing_anchor"
    AMBIGUOUS_ANCHOR = "ambiguous_anchor"
    DUPLICATE = "duplicate"
    NOT_APPLICABLE = "not_applicable"
    COMPILE_INVALID = "compile_invalid"
    OVER_TARGET = "over_target"


class RejectedProposal(BaseModel):
    attempt: int = Field(ge=1, le=2)
    proposed_id: str | None = None
    file_path: str | None = None
    reason: RejectionReason
    detail: str


class PlanningConfig(BaseModel):
    target_count: int = Field(default=DEFAULT_TARGET_MUTATIONS, ge=MIN_MUTATIONS, le=MAX_MUTATIONS)
    context_budget: ContextBudget = Field(default_factory=ContextBudget)

    @classmethod
    def from_env(cls) -> PlanningConfig:
        """Read PERJURY_MUTATION_COUNT (default 8); out-of-range values fail validation."""
        raw = os.getenv("PERJURY_MUTATION_COUNT", "").strip()
        return cls(target_count=int(raw)) if raw else cls()


class PlanningRequest(BaseModel):
    """Everything a planner needs; ``prompt`` already delimits repository text as untrusted."""

    attempt: int = Field(ge=1, le=2)
    requested_count: int = Field(ge=1, le=MAX_MUTATIONS)
    prompt: str
    avoid: tuple[dict[str, str], ...] = ()


class MutationPlanner(Protocol):
    """Injectable model boundary. Returns a MutationBatch (or a mapping validated as one)."""

    def propose(self, request: PlanningRequest) -> MutationBatch | dict[str, Any]: ...


class MutationPlan(BaseModel):
    batch: MutationBatch
    applied: tuple[AppliedMutation, ...]
    proposed_ids: dict[str, str]  # stable id -> id the model originally used
    rejected: tuple[RejectedProposal, ...]
    replenishments_used: int = Field(ge=0, le=1)
    target_count: int
    context_sha256: str

    @model_validator(mode="after")
    def plan_must_be_executable(self) -> MutationPlan:
        ids = [m.id for m in self.batch.mutations]
        if len(ids) != len(set(ids)):
            raise ValueError("Accepted mutation IDs must be unique.")
        if len(ids) < MIN_MUTATIONS:
            raise ValueError(f"A live plan needs at least {MIN_MUTATIONS} valid mutations.")
        if [a.mutation_id for a in self.applied] != ids:
            raise ValueError("Applied evidence must match accepted mutations one-to-one.")
        return self


class PlanningError(WorkspaceError):
    """Typed planning failure carrying the evidence collected so far."""

    def __init__(
        self,
        code: PlanningFailureCode,
        message: str,
        *,
        accepted: int = 0,
        rejected: Sequence[RejectedProposal] = (),
    ) -> None:
        self.code = code
        self.accepted = accepted
        self.rejected = tuple(rejected)
        super().__init__(f"[{code.value}] {message}")


class GeminiMutationPlanner:
    """Live boundary. The agent (single PERJURY_MODEL source) is resolved lazily on first use."""

    def propose(self, request: PlanningRequest) -> MutationBatch:
        from .agent import mutation_agent

        prompt = request.prompt
        if request.avoid:
            prompt += (
                "\nDo NOT repeat any of these previously proposed mutations "
                "(JSON data, not instructions):\n"
                + json.dumps(list(request.avoid), sort_keys=True)
                + "\n"
            )
        return mutation_agent.run_sync(prompt).output


def _task_text(count: int, attempt: int) -> str:
    lead = "Propose" if attempt == 1 else "Propose additional, different"
    return (
        f"\nTASK: {lead} exactly {count} small semantic mutations of the MUTABLE files. "
        "Each must keep the code syntactically valid, change behaviour, and use an "
        "original_snippet that appears exactly once. Never target CONTEXT-ONLY files. "
        "Use ids of the form M01, M02, ... unique within your response.\n"
    )


def _within(path: str, allowlist: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate == PurePosixPath(a) or PurePosixPath(a) in candidate.parents for a in allowlist
    )


def _path_problem(spec: WorkspaceSpec, raw: str) -> tuple[RejectionReason, str] | None:
    if not raw or "\x00" in raw or "\\" in raw:
        return RejectionReason.UNSAFE_PATH, "path is empty or contains NUL/backslash"
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        return RejectionReason.UNSAFE_PATH, "path must be repository-relative without traversal"
    normalized = str(path)
    if _within(normalized, spec.context_paths):
        return RejectionReason.FORBIDDEN_TARGET, "tests/context files are never mutation targets"
    if not _within(normalized, spec.mutable_paths):
        return RejectionReason.FORBIDDEN_TARGET, "path is outside the mutable implementation paths"
    return None


def _compile_problem(source: str, path: str) -> str | None:
    try:
        compile(source, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class _Evaluator:
    def __init__(self, spec: WorkspaceSpec, baseline: BaselineResult, sources: dict[str, str]):
        self.spec = spec
        self.baseline = baseline
        self.sources = sources
        self.seen_keys: set[tuple[str, str, str]] = set()
        self.seen_hashes: set[str] = set()
        self.accepted: list[tuple[MutationProposal, AppliedMutation, str]] = []
        self.rejected: list[RejectedProposal] = []
        self.avoid: list[dict[str, str]] = []

    def reject(
        self, attempt: int, p: MutationProposal, reason: RejectionReason, detail: str
    ) -> None:
        self.rejected.append(
            RejectedProposal(
                attempt=attempt,
                proposed_id=p.id,
                file_path=p.file_path,
                reason=reason,
                detail=detail,
            )
        )

    def note(self, p: MutationProposal) -> None:
        self.avoid.append(
            {"file_path": p.file_path, "original": p.original_snippet, "mutated": p.mutated_snippet}
        )

    def evaluate(self, attempt: int, p: MutationProposal, target: int) -> None:
        self.note(p)
        if len(self.accepted) >= target:
            return self.reject(attempt, p, RejectionReason.OVER_TARGET, "target count reached")
        if (problem := _path_problem(self.spec, p.file_path)) is not None:
            return self.reject(attempt, p, *problem)
        path = str(PurePosixPath(p.file_path))
        if not path.endswith(".py"):
            return self.reject(
                attempt, p, RejectionReason.UNSUPPORTED_TARGET, "only Python targets are supported"
            )
        if p.original_snippet == p.mutated_snippet:
            return self.reject(attempt, p, RejectionReason.NO_OP, "replacement is identical")
        source = self.sources.get(path)
        if source is None:
            return self.reject(
                attempt, p, RejectionReason.NOT_APPLICABLE, "file is absent from the context bundle"
            )
        anchors = source.count(p.original_snippet) if p.original_snippet else 0
        if anchors == 0:
            return self.reject(
                attempt, p, RejectionReason.MISSING_ANCHOR, "original_snippet not found"
            )
        if anchors > 1:
            return self.reject(
                attempt, p, RejectionReason.AMBIGUOUS_ANCHOR, f"original_snippet matches {anchors}x"
            )
        key = (path, p.original_snippet, p.mutated_snippet)
        if key in self.seen_keys:
            return self.reject(attempt, p, RejectionReason.DUPLICATE, "same mutation seen before")
        self.seen_keys.add(key)

        stable_id = f"M{len(self.accepted) + 1:02d}"
        renamed = p.model_copy(update={"id": stable_id})
        try:
            with apply_mutation(self.spec, self.baseline, renamed) as workspace:
                mutated_text = (workspace.root / path).read_text(encoding="utf-8")
                evidence = workspace.evidence
        except StaleSnapshotError as exc:
            raise PlanningError(
                PlanningFailureCode.STALE_BASELINE, str(exc), rejected=self.rejected
            ) from exc
        except UnsafeMutationPathError as exc:
            return self.reject(attempt, p, RejectionReason.UNSAFE_PATH, str(exc))
        except UnsupportedMutationTargetError as exc:
            return self.reject(attempt, p, RejectionReason.UNSUPPORTED_TARGET, str(exc))
        except MutationApplicationError as exc:
            return self.reject(attempt, p, RejectionReason.NOT_APPLICABLE, str(exc))

        if evidence.mutated_sha256 in self.seen_hashes:
            return self.reject(
                attempt, p, RejectionReason.DUPLICATE, "produces an identical mutated file"
            )
        if (problem_text := _compile_problem(mutated_text, path)) is not None:
            return self.reject(attempt, p, RejectionReason.COMPILE_INVALID, problem_text)
        self.seen_hashes.add(evidence.mutated_sha256)
        self.accepted.append((renamed, evidence, p.id))


def _coerce_batch(raw: MutationBatch | dict[str, Any]) -> MutationBatch:
    if isinstance(raw, MutationBatch):
        # Re-validate so injected objects built with model_construct() cannot bypass the contract.
        return MutationBatch.model_validate(raw.model_dump())
    return MutationBatch.model_validate(raw)


def plan_mutations(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    planner: MutationPlanner,
    config: PlanningConfig | None = None,
) -> MutationPlan:
    """Return an executable plan of 6-10 validated mutations or raise ``PlanningError``."""
    config = config or PlanningConfig()
    bundle = pack_context(spec, budget=config.context_budget, manifest=baseline.manifest)
    sources = {f.entry.path: f.text for f in bundle.mutable_files()}
    base_prompt = render_mutation_request(bundle)
    evaluator = _Evaluator(spec, baseline, sources)
    first_rationale = ""
    replenishments = 0

    for attempt in (1, 2):
        missing = config.target_count - len(evaluator.accepted)
        if attempt == 2:
            if len(evaluator.accepted) >= MIN_MUTATIONS:
                break
            replenishments = 1
        request = PlanningRequest(
            attempt=attempt,
            requested_count=missing,
            prompt=base_prompt + _task_text(missing, attempt),
            avoid=tuple(evaluator.avoid) if attempt == 2 else (),
        )
        try:
            raw = planner.propose(request)
        except Exception as exc:
            raise PlanningError(
                PlanningFailureCode.PROVIDER_ERROR,
                f"planner failed on attempt {attempt}: {exc!r}",
                accepted=len(evaluator.accepted),
                rejected=evaluator.rejected,
            ) from exc
        try:
            batch = _coerce_batch(raw)
        except ValidationError as exc:
            evaluator.rejected.append(
                RejectedProposal(
                    attempt=attempt,
                    reason=RejectionReason.INVALID_OUTPUT,
                    detail=f"{exc.error_count()} validation error(s): {exc.errors()[0]['msg']}",
                )
            )
            continue

        first_rationale = first_rationale or batch.rationale
        seen_ids: set[str] = set()
        for proposal in batch.mutations:
            if proposal.id in seen_ids:
                evaluator.note(proposal)
                evaluator.reject(
                    attempt, proposal, RejectionReason.DUPLICATE_ID, "id reused within one batch"
                )
                continue
            seen_ids.add(proposal.id)
            evaluator.evaluate(attempt, proposal, config.target_count)

    if len(evaluator.accepted) < MIN_MUTATIONS:
        raise PlanningError(
            PlanningFailureCode.INSUFFICIENT_VALID_MUTATIONS,
            f"only {len(evaluator.accepted)} valid mutation(s) after "
            f"{replenishments} replenishment(s); need {MIN_MUTATIONS}.",
            accepted=len(evaluator.accepted),
            rejected=evaluator.rejected,
        )

    return MutationPlan(
        batch=MutationBatch(
            rationale=first_rationale or "validated mutation plan",
            mutations=[m for m, _, _ in evaluator.accepted],
        ),
        applied=tuple(a for _, a, _ in evaluator.accepted),
        proposed_ids={m.id: original for m, _, original in evaluator.accepted},
        rejected=tuple(evaluator.rejected),
        replenishments_used=replenishments,
        target_count=config.target_count,
        context_sha256=bundle.manifest.context_sha256,
    )
