"""First-pass mutation execution and scoring (#11).

Applies each accepted mutation from a ``MutationPlan`` to its own isolated workspace (#5), runs
them through the existing bounded fan-out (#7) with the shared execution taxonomy (#8), and
returns exactly one terminal trial per accepted mutation ID plus a derived mutation score.

Score policy: the denominator is KILLED + SURVIVED only. INVALID / TIMEOUT / INFRA_ERROR trials
stay visible with their evidence but are excluded. With no valid trial the score is explicitly
``inconclusive`` (``None``), never 0% and never a division by zero.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from .contracts import (
    BaselineResult,
    ExecutionOutcome,
    ExecutionResult,
    MutationStatus,
    WorkspaceSpec,
)
from .fanout import FanoutJob, FanoutJobResult, FanoutResult, run_fanout
from .mutation import (
    MutationApplicationError,
    MutationWorkspace,
    StaleSnapshotError,
    apply_mutation,
)
from .planning import MutationPlan
from .workspace import WorkspaceError, WorkspaceExecutor


class MutationExecutionError(WorkspaceError):
    """Raised when the executed result set cannot be reconciled with the accepted plan."""


class FanoutRunner(Protocol):
    def __call__(
        self,
        jobs: Sequence[FanoutJob],
        executor: WorkspaceExecutor,
        *,
        max_concurrency: int | None = ...,
        cancel_event: threading.Event | None = ...,
        on_result: Callable[[FanoutJobResult], None] | None = ...,
    ) -> FanoutResult: ...


class MutationScore(BaseModel):
    """Derived metric. ``score`` is None exactly when there is no valid denominator."""

    killed: int = Field(ge=0)
    survived: int = Field(ge=0)
    invalid: int = Field(ge=0)
    timeout: int = Field(ge=0)
    infra_error: int = Field(ge=0)
    valid_total: int = Field(ge=0)
    excluded_total: int = Field(ge=0)
    state: Literal["scored", "inconclusive"]
    score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def score_must_follow_the_valid_denominator(self) -> MutationScore:
        if self.valid_total != self.killed + self.survived:
            raise ValueError("valid_total must equal killed + survived.")
        if self.excluded_total != self.invalid + self.timeout + self.infra_error:
            raise ValueError("excluded_total must equal invalid + timeout + infra_error.")
        if self.valid_total == 0:
            if self.state != "inconclusive" or self.score is not None:
                raise ValueError("A zero valid denominator must be inconclusive with no score.")
        elif self.state != "scored" or self.score != self.killed / self.valid_total:
            raise ValueError("A scored result must equal killed / (killed + survived).")
        return self


def compute_mutation_score(statuses: Iterable[MutationStatus]) -> MutationScore:
    """Score from projected statuses; reused by the #25 re-score so both passes agree."""
    counts = Counter(statuses)
    killed = counts[MutationStatus.KILLED]
    survived = counts[MutationStatus.SURVIVED]
    valid = killed + survived
    excluded = sum(
        counts[s]
        for s in (MutationStatus.INVALID, MutationStatus.TIMEOUT, MutationStatus.INFRA_ERROR)
    )
    return MutationScore(
        killed=killed,
        survived=survived,
        invalid=counts[MutationStatus.INVALID],
        timeout=counts[MutationStatus.TIMEOUT],
        infra_error=counts[MutationStatus.INFRA_ERROR],
        valid_total=valid,
        excluded_total=excluded,
        state="scored" if valid else "inconclusive",
        score=(killed / valid) if valid else None,
    )


class MutationTrial(BaseModel):
    """One terminal result for one accepted mutation, with retained evidence."""

    mutation_id: str
    target_path: str
    diff: str
    status: MutationStatus
    execution: ExecutionResult
    duration_ms: int = Field(ge=0)
    started_offset_ms: int | None = Field(default=None, ge=0)
    finished_offset_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def status_must_project_the_execution_outcome(self) -> MutationTrial:
        if self.status is not self.execution.mutation_status:
            raise ValueError("Trial status must be the projection of its execution outcome.")
        return self


class MutationExecutionReport(BaseModel):
    workspace_id: str
    base_manifest_sha256: str
    context_sha256: str
    trials: tuple[MutationTrial, ...] = Field(min_length=1)
    score: MutationScore
    wall_clock_ms: int = Field(ge=0)
    max_concurrency: int = Field(ge=0)
    peak_concurrency: int = Field(ge=0)

    @model_validator(mode="after")
    def report_must_be_internally_consistent(self) -> MutationExecutionReport:
        ids = [t.mutation_id for t in self.trials]
        if len(ids) != len(set(ids)):
            raise ValueError("Each mutation ID must have exactly one terminal trial.")
        if self.score != compute_mutation_score(t.status for t in self.trials):
            raise ValueError("Score does not match the retained trials.")
        return self

    def survivors(self) -> tuple[MutationTrial, ...]:
        return tuple(t for t in self.trials if t.status is MutationStatus.SURVIVED)


def _apply_error_result(mutation_id: str, detail: str) -> ExecutionResult:
    return ExecutionResult(
        execution_id=f"mutation:{mutation_id}",
        outcome=ExecutionOutcome.INVALID,
        duration_ms=0,
        failure_detail=f"mutation could not be applied to the baseline snapshot: {detail}",
    )


def execute_mutation_plan(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: MutationPlan,
    executor: WorkspaceExecutor,
    *,
    max_concurrency: int | None = None,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[FanoutJobResult], None] | None = None,
    fanout_runner: FanoutRunner = run_fanout,
) -> MutationExecutionReport:
    """Run every accepted mutation once and return the reconciled first-pass report.

    Each mutation is applied to its own disposable copy of the same baseline snapshot. The
    result ID set must equal the accepted ID set; any mismatch raises MutationExecutionError.
    """
    accepted = [m.id for m in plan.batch.mutations]
    if not accepted:
        raise MutationExecutionError("Plan contains no accepted mutations.")

    workspaces: dict[str, MutationWorkspace] = {}
    apply_failures: dict[str, ExecutionResult] = {}
    try:
        for proposal, planned in zip(plan.batch.mutations, plan.applied, strict=True):
            try:
                workspace = apply_mutation(spec, baseline, proposal)
            except StaleSnapshotError:
                raise
            except MutationApplicationError as exc:
                apply_failures[proposal.id] = _apply_error_result(proposal.id, str(exc))
                continue
            if workspace.evidence.mutated_sha256 != planned.mutated_sha256:
                workspace.cleanup()
                raise MutationExecutionError(
                    f"Mutation {proposal.id} no longer reproduces its planned bytes."
                )
            workspaces[proposal.id] = workspace

        jobs = [FanoutJob(mutation_id, w.spec, w.manifest) for mutation_id, w in workspaces.items()]
        fanned: FanoutResult | None = None
        if jobs:
            fanned = fanout_runner(
                jobs,
                executor,
                max_concurrency=max_concurrency,
                cancel_event=cancel_event,
                on_result=on_result,
            )
        by_id = {r.mutation_id: r for r in fanned.results} if fanned else {}
        if fanned and len(by_id) != len(fanned.results):
            raise MutationExecutionError("Fan-out returned duplicate mutation IDs.")
        expected = {job.mutation_id for job in jobs}
        if set(by_id) != expected:
            raise MutationExecutionError(
                f"Fan-out result IDs {sorted(by_id)} do not match executed IDs {sorted(expected)}."
            )

        trials: list[MutationTrial] = []
        applied_by_id = {a.mutation_id: a for a in plan.applied}
        for mutation_id in accepted:
            evidence = applied_by_id[mutation_id]
            if mutation_id in apply_failures:
                execution = apply_failures[mutation_id]
                timing: dict[str, int | None] = {"started": None, "finished": None}
            else:
                item = by_id[mutation_id]
                execution = item.execution
                timing = {"started": item.started_offset_ms, "finished": item.finished_offset_ms}
            trials.append(
                MutationTrial(
                    mutation_id=mutation_id,
                    target_path=evidence.target_path,
                    diff=evidence.diff,
                    status=execution.mutation_status,
                    execution=execution,
                    duration_ms=execution.duration_ms,
                    started_offset_ms=timing["started"],
                    finished_offset_ms=timing["finished"],
                )
            )
    finally:
        for workspace in workspaces.values():
            workspace.cleanup()

    return MutationExecutionReport(
        workspace_id=spec.workspace_id,
        base_manifest_sha256=baseline.manifest_sha256,
        context_sha256=plan.context_sha256,
        trials=tuple(trials),
        score=compute_mutation_score(t.status for t in trials),
        wall_clock_ms=fanned.wall_clock_ms if fanned else 0,
        max_concurrency=fanned.max_concurrency if fanned else 0,
        peak_concurrency=fanned.peak_concurrency if fanned else 0,
    )
