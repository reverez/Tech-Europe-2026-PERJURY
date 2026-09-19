"""Same-batch re-score after a verified hardening (#25).

Starting only from a VERIFIED candidate, the exact same validated mutation batch is replayed
through the existing execution path (``execute_mutation_plan``) with the byte-identical candidate
present in every isolated mutant workspace. No new batch can be substituted: the replay is built
from the same ``MutationPlan`` and its identity is checked against the first pass.

The score delta is authoritative and computed here (killed / (killed + survived) on both sides;
INVALID/TIMEOUT/INFRA_ERROR excluded). If either side has no valid trial the delta is
unavailable, never a fabricated number. If the selected verified mutant is not killed on replay,
or a previously killed mutant now survives, the replay is flagged INCONSISTENT and no
improvement is claimed.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .candidate import CandidatePlan
from .contracts import BaselineResult, MutationStatus, WorkspaceSpec
from .execution import (
    FanoutRunner,
    MutationExecutionReport,
    MutationScore,
    compute_mutation_score,
    execute_mutation_plan,
)
from .fanout import FanoutJobResult, run_fanout
from .hardening import HardeningVerification
from .planning import MutationPlan
from .workspace import WorkspaceError, WorkspaceExecutor

Direction = Literal["improved", "unchanged", "regressed", "unavailable"]


class RescoreError(WorkspaceError):
    """The re-score preconditions do not hold (not verified / identities do not match)."""


class RescoreStatus(StrEnum):
    CONFIRMED = "confirmed"
    INCONSISTENT = "inconsistent"


def batch_identity(plan: MutationPlan) -> str:
    """Stable hash of the accepted batch: IDs, targets and exact before/after file bytes."""
    digest = hashlib.sha256()
    digest.update(plan.context_sha256.encode())
    for applied in plan.applied:
        for part in (
            applied.mutation_id,
            applied.target_path,
            applied.original_sha256,
            applied.mutated_sha256,
            applied.base_manifest_sha256,
        ):
            digest.update(b"\0" + part.encode())
    return f"sha256:{digest.hexdigest()}"


class ScoreComparison(BaseModel):
    """Authoritative before/after evidence; consumers must not recompute it."""

    batch_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mutation_ids: tuple[str, ...]
    before: MutationScore
    after: MutationScore
    delta: float | None = None
    direction: Direction
    killed_delta: int
    newly_killed_ids: tuple[str, ...] = ()
    newly_survived_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def delta_must_follow_both_scores(self) -> ScoreComparison:
        both = self.before.score is not None and self.after.score is not None
        if not both:
            if self.delta is not None or self.direction != "unavailable":
                raise ValueError("Delta must be unavailable when either side has no valid trial.")
            return self
        expected = self.after.score - self.before.score
        if self.delta is None or abs(self.delta - expected) > 1e-12:
            raise ValueError("Delta must equal after.score - before.score.")
        want = "improved" if expected > 0 else "regressed" if expected < 0 else "unchanged"
        if self.direction != want:
            raise ValueError("Direction contradicts the delta.")
        return self


class RescoreReport(BaseModel):
    status: RescoreStatus
    selected_mutation_id: str
    candidate_path: str
    candidate_sha256: str
    comparison: ScoreComparison
    before_report: MutationExecutionReport
    after_report: MutationExecutionReport
    selected_before: MutationStatus
    selected_after: MutationStatus
    inconsistencies: tuple[str, ...] = ()
    message: str

    @model_validator(mode="after")
    def status_must_match_inconsistencies(self) -> RescoreReport:
        if (self.status is RescoreStatus.CONFIRMED) == bool(self.inconsistencies):
            raise ValueError("Status must be confirmed exactly when there are no inconsistencies.")
        return self

    @property
    def improvement_verified(self) -> bool:
        return (
            self.status is RescoreStatus.CONFIRMED
            and self.comparison.direction == "improved"
            and self.selected_after is MutationStatus.KILLED
        )


def compare_scores(
    plan: MutationPlan,
    before: MutationExecutionReport,
    after: MutationExecutionReport,
) -> ScoreComparison:
    before_status = {t.mutation_id: t.status for t in before.trials}
    after_status = {t.mutation_id: t.status for t in after.trials}
    b_score = compute_mutation_score(before_status.values())
    a_score = compute_mutation_score(after_status.values())
    delta = (
        a_score.score - b_score.score
        if a_score.score is not None and b_score.score is not None
        else None
    )
    direction: Direction = (
        "unavailable"
        if delta is None
        else "improved"
        if delta > 0
        else "regressed"
        if delta < 0
        else "unchanged"
    )
    ids = tuple(m.id for m in plan.batch.mutations)
    K, S = MutationStatus.KILLED, MutationStatus.SURVIVED
    return ScoreComparison(
        batch_sha256=batch_identity(plan),
        mutation_ids=ids,
        before=b_score,
        after=a_score,
        delta=delta,
        direction=direction,
        killed_delta=a_score.killed - b_score.killed,
        newly_killed_ids=tuple(i for i in ids if before_status[i] is S and after_status[i] is K),
        newly_survived_ids=tuple(i for i in ids if before_status[i] is K and after_status[i] is S),
    )


def rescore_with_candidate(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: MutationPlan,
    before: MutationExecutionReport,
    verification: HardeningVerification,
    candidate: CandidatePlan,
    executor: WorkspaceExecutor,
    *,
    max_concurrency: int | None = None,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[FanoutJobResult], None] | None = None,
    fanout_runner: FanoutRunner = run_fanout,
) -> RescoreReport:
    """Replay the same batch with the verified candidate and return the authoritative comparison."""
    if verification.result.verdict != "verified" or not verification.result.evidence.verified:
        raise RescoreError("Re-score starts only from a VERIFIED candidate.")
    selected = verification.result.mutation_id
    ids = [m.id for m in plan.batch.mutations]
    if selected not in ids:
        raise RescoreError(f"Verified mutation {selected!r} is not in the accepted batch.")
    if (
        candidate.mutation_id != selected
        or verification.candidate_sha256 != candidate.sha256
        or verification.candidate_path != candidate.candidate_path
        or verification.result.evidence.candidate_sha256 != candidate.sha256
    ):
        raise RescoreError("Verified evidence does not describe this candidate.")
    if (
        before.context_sha256 != plan.context_sha256
        or before.base_manifest_sha256 != baseline.manifest_sha256
        or candidate.base_manifest_sha256 != baseline.manifest_sha256
        or [t.mutation_id for t in before.trials] != ids
    ):
        raise RescoreError("First-pass report does not belong to this plan/baseline.")

    after = execute_mutation_plan(
        spec,
        baseline,
        plan,
        executor,
        max_concurrency=max_concurrency,
        cancel_event=cancel_event,
        on_result=on_result,
        fanout_runner=fanout_runner,
        candidate=candidate,
    )

    comparison = compare_scores(plan, before, after)
    by_before = {t.mutation_id: t for t in before.trials}
    by_after = {t.mutation_id: t for t in after.trials}
    problems: list[str] = []
    if [t.mutation_id for t in after.trials] != ids:
        problems.append("replay does not cover exactly the accepted batch")
    for mutation_id in ids:
        if by_before[mutation_id].diff != by_after[mutation_id].diff:
            problems.append(f"{mutation_id}: replayed mutation differs from the first pass")
    if by_before[selected].status is not MutationStatus.SURVIVED:
        problems.append(f"{selected}: was not a survivor in the first pass")
    if by_after[selected].status is not MutationStatus.KILLED:
        problems.append(
            f"{selected}: verified mutant is {by_after[selected].status.value}, not killed, "
            "on replay"
        )
    problems += [
        f"{i}: killed in the first pass but survived after adding a test"
        for i in comparison.newly_survived_ids
    ]

    status = RescoreStatus.INCONSISTENT if problems else RescoreStatus.CONFIRMED
    if problems:
        message = "Re-score is inconsistent; no improvement is claimed: " + "; ".join(problems)
    elif comparison.direction == "unavailable":
        message = "Re-score confirmed the selected mutant, but a score delta is unavailable."
    else:
        message = (
            f"Same-batch re-score: {comparison.before.score:.3f} -> {comparison.after.score:.3f} "
            f"({comparison.direction})."
        )
    return RescoreReport(
        status=status,
        selected_mutation_id=selected,
        candidate_path=candidate.candidate_path,
        candidate_sha256=candidate.sha256,
        comparison=comparison,
        before_report=before,
        after_report=after,
        selected_before=by_before[selected].status,
        selected_after=by_after[selected].status,
        inconsistencies=tuple(problems),
        message=message,
    )
