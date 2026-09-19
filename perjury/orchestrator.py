"""The single typed entrypoint for the complete PERJURY loop (#15).

    baseline -> context -> mutation plan -> mutation execution -> survivor analysis
             -> generated candidate -> two-world verification -> same-batch re-score -> terminal

Every stage delegates to an existing primitive (``run_baseline``, ``pack_context``,
``plan_mutations``, ``execute_mutation_plan``, ``select_hardening_target``, ``plan_candidate`` /
``preflight_candidate``, ``verify_candidate``, ``rescore_with_candidate``); this module adds only
the run state machine, correlated typed events, per-stage timing/evidence references and typed
terminal states. It contains no UI logic and no second execution path. Models are injected
(planner / analyzer / generator), so the identical path runs deterministically in CI and live.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .analysis import (
    SelectionStatus,
    SurvivorAnalysisError,
    SurvivorAnalyzer,
    SurvivorSelection,
    select_hardening_target,
)
from .candidate import (
    CandidateEvidence,
    CandidatePlan,
    CandidateRejected,
    plan_candidate,
    preflight_candidate,
)
from .context import ContextError, pack_context
from .contracts import BaselineResult, ExecutionOutcome, WorkspaceSpec
from .execution import MutationExecutionError, MutationExecutionReport, execute_mutation_plan
from .fanout import FanoutJobResult
from .generation import GenerationError, TestGenerator, generate_test
from .hardening import HardeningVerification, WorldConsistencyError, verify_candidate
from .mutation import MutationApplicationError
from .planning import MutationPlan, MutationPlanner, PlanningConfig, PlanningError, plan_mutations
from .rescore import (
    RescoreError,
    RescoreReport,
    RescoreStatus,
    batch_identity,
    rescore_with_candidate,
)
from .workspace import (
    BaselineNotReadyError,
    WorkspaceError,
    WorkspaceExecutor,
    run_baseline,
)


class RunStage(StrEnum):
    """Working stages in pipeline order, then the four terminal states."""

    CREATED = "created"
    BASELINE = "baseline"
    CONTEXT = "context"
    PLANNING = "planning"
    MUTATION_EXECUTION = "mutation_execution"
    SURVIVOR_ANALYSIS = "survivor_analysis"
    TEST_GENERATION = "test_generation"
    VERIFICATION = "verification"
    RESCORING = "rescoring"
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


WORKING_STAGES: tuple[RunStage, ...] = (
    RunStage.CREATED,
    RunStage.BASELINE,
    RunStage.CONTEXT,
    RunStage.PLANNING,
    RunStage.MUTATION_EXECUTION,
    RunStage.SURVIVOR_ANALYSIS,
    RunStage.TEST_GENERATION,
    RunStage.VERIFICATION,
    RunStage.RESCORING,
)
TERMINAL_STAGES: frozenset[RunStage] = frozenset(
    {RunStage.VERIFIED, RunStage.REJECTED, RunStage.INCONCLUSIVE, RunStage.FAILED}
)

# Monotonic: a working stage may only advance to the next working stage or to any terminal state.
ALLOWED_NEXT: dict[RunStage, frozenset[RunStage]] = {
    stage: frozenset(
        ({WORKING_STAGES[i + 1]} if i + 1 < len(WORKING_STAGES) else set()) | TERMINAL_STAGES
    )
    for i, stage in enumerate(WORKING_STAGES)
} | {stage: frozenset() for stage in TERMINAL_STAGES}


class TerminalReason(StrEnum):
    VERIFIED = "verified"
    # rejected
    CANDIDATE_INVALID = "candidate_invalid"
    CANDIDATE_FAILED_ORIGINAL = "candidate_failed_original"
    # inconclusive
    NO_VALID_MUTATION_OUTCOMES = "no_valid_mutation_outcomes"
    NO_ACTIONABLE_SURVIVOR = "no_actionable_survivor"
    ALL_POSSIBLY_EQUIVALENT = "all_possibly_equivalent"
    PREFLIGHT_UNAVAILABLE = "preflight_unavailable"
    MUTANT_NOT_KILLED = "mutant_not_killed"
    VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
    RESCORE_INCONSISTENT = "rescore_inconsistent"
    # failed
    BASELINE_NOT_READY = "baseline_not_ready"
    CONTEXT_FAILED = "context_failed"
    PLANNING_FAILED = "planning_failed"
    EXECUTION_FAILED = "execution_failed"
    ANALYSIS_FAILED = "analysis_failed"
    GENERATION_FAILED = "generation_failed"
    WORLD_INCONSISTENT = "world_inconsistent"
    RESCORE_PRECONDITION = "rescore_precondition"
    UNEXPECTED_ERROR = "unexpected_error"


class RunEventType(StrEnum):
    RUN_STARTED = "run.started"
    BASELINE_COMPLETED = "baseline.completed"
    CONTEXT_COMPLETED = "context.completed"
    PLAN_COMPLETED = "plan.completed"
    MUTATION_COMPLETED = "mutation.completed"
    EXECUTION_COMPLETED = "execution.completed"
    SURVIVOR_SELECTED = "survivor.selected"
    ANALYSIS_COMPLETED = "analysis.completed"
    TEST_PROPOSED = "test.proposed"
    CANDIDATE_READY = "candidate.ready"
    VERIFICATION_COMPLETED = "verification.completed"
    RESCORE_MUTATION_COMPLETED = "rescore.mutation.completed"
    RESCORE_COMPLETED = "rescore.completed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class InvalidTransitionError(WorkspaceError):
    """Raised when a stage transition would break monotonic run-state ordering."""


class RunEvent(BaseModel):
    """Ordered, correlated, serializable event for API/observability/UI consumers."""

    run_id: str
    seq: int = Field(ge=1)
    type: RunEventType
    stage: RunStage
    at_ms: int = Field(ge=0)
    correlation_id: str
    mutation_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class StageRecord(BaseModel):
    stage: RunStage
    started_ms: int = Field(ge=0)
    ended_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    status: str  # "ok" | "terminal" | "failed"
    evidence: dict[str, str] = Field(default_factory=dict)


class RunConfig(BaseModel):
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    max_concurrency: int | None = Field(default=None, ge=1, le=10)


class RunResult(BaseModel):
    """Authoritative, serializable outcome of one run."""

    run_id: str
    state: RunStage
    reason: TerminalReason
    message: str
    total_ms: int = Field(ge=0)
    stages: tuple[StageRecord, ...]
    events: tuple[RunEvent, ...]
    baseline_manifest_sha256: str | None = None
    baseline_outcome: str | None = None
    context_sha256: str | None = None
    batch_sha256: str | None = None
    selected_mutation_id: str | None = None
    plan: MutationPlan | None = None
    first_pass: MutationExecutionReport | None = None
    selection: SurvivorSelection | None = None
    candidate: CandidateEvidence | None = None
    verification: HardeningVerification | None = None
    rescore: RescoreReport | None = None
    observer_errors: int = 0

    @property
    def improvement_verified(self) -> bool:
        return self.rescore is not None and self.rescore.improvement_verified

    @model_validator(mode="after")
    def result_must_be_coherent(self) -> RunResult:
        if self.state not in TERMINAL_STAGES:
            raise ValueError("A run result must be in a terminal state.")
        seqs = [e.seq for e in self.events]
        if seqs != list(range(1, len(seqs) + 1)):
            raise ValueError("Events must be numbered 1..N without gaps.")
        if any(a.at_ms > b.at_ms for a, b in zip(self.events, self.events[1:], strict=False)):
            raise ValueError("Event timestamps must be monotonic.")
        if any(e.run_id != self.run_id for e in self.events):
            raise ValueError("Every event must carry the run ID.")
        order = [WORKING_STAGES.index(s.stage) for s in self.stages]
        if order != sorted(set(order)):
            raise ValueError("Stage records must be strictly ordered.")
        last = self.events[-1] if self.events else None
        if last is None or last.stage is not self.state:
            raise ValueError("The final event must report the terminal state.")
        if (last.type is RunEventType.RUN_FAILED) != (self.state is RunStage.FAILED):
            raise ValueError("Only failed runs end with run.failed.")
        if self.state is RunStage.VERIFIED and not (
            self.verification is not None
            and self.verification.result.verdict == "verified"
            and self.rescore is not None
            and self.rescore.status is RescoreStatus.CONFIRMED
        ):
            raise ValueError("VERIFIED requires a verified candidate and a confirmed re-score.")
        return self


class _Stop(Exception):
    def __init__(self, state: RunStage, reason: TerminalReason, message: str) -> None:
        self.state, self.reason, self.message = state, reason, message
        super().__init__(message)


class _StageHandle:
    def __init__(self) -> None:
        self.evidence: dict[str, str] = {}


class _Run:
    """Owns the monotonic state machine, event numbering and stage records for one run."""

    def __init__(
        self,
        run_id: str,
        clock: Callable[[], float],
        on_event: Callable[[RunEvent], None] | None,
    ) -> None:
        self.run_id = run_id
        self._clock = clock
        self._t0 = clock()
        self._on_event = on_event
        self.stage = RunStage.CREATED
        self.events: list[RunEvent] = []
        self.stages: list[StageRecord] = []
        self.observer_errors = 0

    def now_ms(self) -> int:
        return max(0, int((self._clock() - self._t0) * 1000))

    def transition(self, to: RunStage) -> None:
        if to not in ALLOWED_NEXT[self.stage]:
            raise InvalidTransitionError(f"Invalid run transition {self.stage.value} -> {to.value}")
        self.stage = to

    def emit(
        self,
        type_: RunEventType,
        data: dict[str, Any] | None = None,
        *,
        mutation_id: str | None = None,
    ) -> None:
        correlation = f"{self.run_id}:{self.stage.value}" + (
            f":{mutation_id}" if mutation_id else ""
        )
        event = RunEvent(
            run_id=self.run_id,
            seq=len(self.events) + 1,
            type=type_,
            stage=self.stage,
            at_ms=self.now_ms(),
            correlation_id=correlation,
            mutation_id=mutation_id,
            data=data or {},
        )
        self.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - an observer must never break the run
                self.observer_errors += 1

    @contextmanager
    def stage_scope(self, stage: RunStage) -> Iterator[_StageHandle]:
        self.transition(stage)
        handle = _StageHandle()
        started = self.now_ms()
        status = "ok"
        try:
            yield handle
        except _Stop:
            status = "terminal"
            raise
        except BaseException:
            status = "failed"
            raise
        finally:
            ended = self.now_ms()
            self.stages.append(
                StageRecord(
                    stage=stage,
                    started_ms=started,
                    ended_ms=ended,
                    duration_ms=ended - started,
                    status=status,
                    evidence=handle.evidence,
                )
            )


def _score_payload(score: Any) -> dict[str, Any]:
    return {**score.model_dump(mode="json"), "excluded": score.excluded_total}


def _mutation_payload(item: FanoutJobResult) -> dict[str, Any]:
    return {
        "status": item.status.value,
        "outcome": item.execution.outcome.value,
        "duration_ms": item.duration_ms,
    }


def run_perjury(
    spec: WorkspaceSpec,
    *,
    planner: MutationPlanner,
    analyzer: SurvivorAnalyzer,
    generator: TestGenerator,
    executor: WorkspaceExecutor,
    baseline_executor: WorkspaceExecutor | None = None,
    config: RunConfig | None = None,
    run_id: str | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Run the whole loop once and return a typed terminal result; never raises for run outcomes."""
    config = config or RunConfig()
    run = _Run(run_id or f"run-{uuid.uuid4().hex[:12]}", clock, on_event)
    baseline_executor = baseline_executor or executor

    baseline: BaselineResult | None = None
    plan: MutationPlan | None = None
    first_pass: MutationExecutionReport | None = None
    selection: SurvivorSelection | None = None
    candidate_plan: CandidatePlan | None = None
    candidate: CandidateEvidence | None = None
    verification: HardeningVerification | None = None
    rescore: RescoreReport | None = None
    context_sha: str | None = None

    try:
        run.emit(RunEventType.RUN_STARTED, {"workspace_id": spec.workspace_id})

        with run.stage_scope(RunStage.BASELINE) as st:
            try:
                baseline = run_baseline(spec, baseline_executor)
            except (BaselineNotReadyError, WorkspaceError) as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.BASELINE_NOT_READY, str(exc)) from exc
            st.evidence.update(
                manifest_sha256=baseline.manifest_sha256,
                execution_id=baseline.execution.execution_id,
            )
            run.emit(
                RunEventType.BASELINE_COMPLETED,
                {
                    "outcome": baseline.execution.outcome.value.upper(),
                    "duration_ms": baseline.execution.duration_ms,
                    "summary": baseline.execution.stdout.strip().splitlines()[-1]
                    if baseline.execution.stdout.strip()
                    else "",
                    "manifest_sha256": baseline.manifest_sha256,
                },
            )

        with run.stage_scope(RunStage.CONTEXT) as st:
            try:
                bundle = pack_context(
                    spec, budget=config.planning.context_budget, manifest=baseline.manifest
                )
            except (ContextError, WorkspaceError) as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.CONTEXT_FAILED, str(exc)) from exc
            context_sha = bundle.manifest.context_sha256
            st.evidence.update(context_sha256=context_sha)
            run.emit(
                RunEventType.CONTEXT_COMPLETED,
                {
                    "context_sha256": context_sha,
                    "mutable_paths": list(bundle.manifest.mutable_paths),
                    "context_only_paths": list(bundle.manifest.context_only_paths),
                    "omitted": [o.model_dump(mode="json") for o in bundle.manifest.omitted],
                },
            )

        with run.stage_scope(RunStage.PLANNING) as st:
            try:
                plan = plan_mutations(spec, baseline, planner, config.planning)
            except PlanningError as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.PLANNING_FAILED, str(exc)) from exc
            st.evidence.update(
                batch_sha256=batch_identity(plan),
                accepted=str(len(plan.batch.mutations)),
                rejected=str(len(plan.rejected)),
                replenishments=str(plan.replenishments_used),
            )
            run.emit(
                RunEventType.PLAN_COMPLETED,
                {
                    "batch_sha256": batch_identity(plan),
                    "mutations": [
                        {
                            "id": m.id,
                            "file_path": m.file_path,
                            "description": m.description,
                            "hypothesis": m.hypothesis,
                            "diff": a.diff,
                        }
                        for m, a in zip(plan.batch.mutations, plan.applied, strict=True)
                    ],
                    "rejected": [r.model_dump(mode="json") for r in plan.rejected],
                },
            )

        def on_first_pass(item: FanoutJobResult) -> None:
            run.emit(
                RunEventType.MUTATION_COMPLETED,
                _mutation_payload(item),
                mutation_id=item.mutation_id,
            )

        with run.stage_scope(RunStage.MUTATION_EXECUTION) as st:
            try:
                first_pass = execute_mutation_plan(
                    spec,
                    baseline,
                    plan,
                    executor,
                    max_concurrency=config.max_concurrency,
                    cancel_event=cancel_event,
                    on_result=on_first_pass,
                )
            except (MutationExecutionError, MutationApplicationError, WorkspaceError) as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.EXECUTION_FAILED, str(exc)) from exc
            st.evidence.update(
                trials=str(len(first_pass.trials)),
                wall_clock_ms=str(first_pass.wall_clock_ms),
                score_state=first_pass.score.state,
            )
            run.emit(RunEventType.EXECUTION_COMPLETED, {"score": _score_payload(first_pass.score)})
            if first_pass.score.valid_total == 0:
                raise _Stop(
                    RunStage.INCONCLUSIVE,
                    TerminalReason.NO_VALID_MUTATION_OUTCOMES,
                    "No mutation produced a valid killed/survived outcome; score is inconclusive.",
                )

        with run.stage_scope(RunStage.SURVIVOR_ANALYSIS) as st:
            try:
                selection = select_hardening_target(spec, baseline, plan, first_pass, analyzer)
            except SurvivorAnalysisError as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.ANALYSIS_FAILED, str(exc)) from exc
            st.evidence.update(
                candidates=",".join(selection.candidate_ids), status=selection.status.value
            )
            for analysis in selection.analyses:
                run.emit(
                    RunEventType.ANALYSIS_COMPLETED,
                    analysis.model_dump(mode="json"),
                    mutation_id=analysis.mutation_id,
                )
            if selection.status is SelectionStatus.NO_SURVIVORS:
                raise _Stop(
                    RunStage.INCONCLUSIVE, TerminalReason.NO_ACTIONABLE_SURVIVOR, selection.message
                )
            if selection.status is SelectionStatus.ALL_POSSIBLY_EQUIVALENT:
                raise _Stop(
                    RunStage.INCONCLUSIVE, TerminalReason.ALL_POSSIBLY_EQUIVALENT, selection.message
                )
            assert selection.target is not None
            run.emit(
                RunEventType.SURVIVOR_SELECTED,
                {"label": selection.target.label, "summary": selection.target.summary},
                mutation_id=selection.target.mutation_id,
            )
        selected_id = selection.target.mutation_id
        selected = next(m for m in plan.batch.mutations if m.id == selected_id)

        with run.stage_scope(RunStage.TEST_GENERATION) as st:
            try:
                proposal = generate_test(
                    spec,
                    baseline,
                    plan,
                    selection,
                    generator,
                    config.planning.context_budget,
                )
            except GenerationError as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.GENERATION_FAILED, str(exc)) from exc
            run.emit(
                RunEventType.TEST_PROPOSED,
                proposal.model_dump(mode="json"),
                mutation_id=selected_id,
            )
            try:
                candidate_plan = plan_candidate(spec, baseline, proposal)
                candidate = preflight_candidate(spec, baseline, candidate_plan, executor)
            except CandidateRejected as exc:
                if exc.outcome is ExecutionOutcome.INVALID:
                    raise _Stop(
                        RunStage.REJECTED, TerminalReason.CANDIDATE_INVALID, str(exc)
                    ) from exc
                raise _Stop(
                    RunStage.INCONCLUSIVE, TerminalReason.PREFLIGHT_UNAVAILABLE, str(exc)
                ) from exc
            st.evidence.update(
                candidate_path=candidate_plan.candidate_path, candidate_sha256=candidate_plan.sha256
            )
            run.emit(
                RunEventType.CANDIDATE_READY,
                {
                    "candidate_path": candidate_plan.candidate_path,
                    "sha256": candidate_plan.sha256,
                    "diff": candidate_plan.diff,
                },
                mutation_id=selected_id,
            )

        with run.stage_scope(RunStage.VERIFICATION) as st:
            try:
                verification = verify_candidate(spec, baseline, selected, candidate_plan, executor)
            except (WorldConsistencyError, CandidateRejected, MutationApplicationError) as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.WORLD_INCONSISTENT, str(exc)) from exc
            evidence = verification.result.evidence
            st.evidence.update(
                verdict=verification.result.verdict,
                original_manifest_sha256=str(evidence.original_manifest_sha256),
                mutant_manifest_sha256=str(evidence.mutant_manifest_sha256),
            )
            run.emit(
                RunEventType.VERIFICATION_COMPLETED,
                {
                    "original": evidence.original_outcome.value.upper(),
                    "mutant": evidence.mutant_outcome.value.upper(),
                    "original_duration_ms": evidence.original_duration_ms,
                    "mutant_duration_ms": evidence.mutant_duration_ms,
                    "verdict": verification.result.verdict,
                    "explanation": verification.result.explanation,
                },
                mutation_id=selected_id,
            )
            if verification.result.verdict == "rejected":
                raise _Stop(
                    RunStage.REJECTED,
                    TerminalReason.CANDIDATE_FAILED_ORIGINAL,
                    verification.result.explanation,
                )
            if verification.result.verdict != "verified":
                reason = (
                    TerminalReason.MUTANT_NOT_KILLED
                    if evidence.original_outcome is ExecutionOutcome.PASS
                    else TerminalReason.VERIFICATION_INCONCLUSIVE
                )
                raise _Stop(RunStage.INCONCLUSIVE, reason, verification.result.explanation)

        def on_rescore(item: FanoutJobResult) -> None:
            run.emit(
                RunEventType.RESCORE_MUTATION_COMPLETED,
                _mutation_payload(item),
                mutation_id=item.mutation_id,
            )

        with run.stage_scope(RunStage.RESCORING) as st:
            try:
                rescore = rescore_with_candidate(
                    spec,
                    baseline,
                    plan,
                    first_pass,
                    verification,
                    candidate_plan,
                    executor,
                    max_concurrency=config.max_concurrency,
                    cancel_event=cancel_event,
                    on_result=on_rescore,
                )
            except (RescoreError, MutationExecutionError, WorkspaceError) as exc:
                raise _Stop(RunStage.FAILED, TerminalReason.RESCORE_PRECONDITION, str(exc)) from exc
            comparison = rescore.comparison
            st.evidence.update(batch_sha256=comparison.batch_sha256, status=rescore.status.value)
            run.emit(
                RunEventType.RESCORE_COMPLETED,
                {
                    "status": rescore.status.value,
                    "before": _score_payload(comparison.before),
                    "after": _score_payload(comparison.after),
                    "delta": comparison.delta,
                    "direction": comparison.direction,
                    "batch_sha256": comparison.batch_sha256,
                    "message": rescore.message,
                },
                mutation_id=selected_id,
            )
            if rescore.status is not RescoreStatus.CONFIRMED:
                raise _Stop(
                    RunStage.INCONCLUSIVE, TerminalReason.RESCORE_INCONSISTENT, rescore.message
                )

        stop = _Stop(RunStage.VERIFIED, TerminalReason.VERIFIED, rescore.message)
    except _Stop as caught:
        stop = caught
    except InvalidTransitionError:
        raise
    except Exception as exc:  # noqa: BLE001 - the run must end in a typed terminal state
        stop = _Stop(
            RunStage.FAILED, TerminalReason.UNEXPECTED_ERROR, f"{type(exc).__name__}: {exc}"
        )

    run.transition(stop.state)
    if stop.state is RunStage.FAILED:
        run.emit(RunEventType.RUN_FAILED, {"code": stop.reason.value, "message": stop.message})
    else:
        run.emit(
            RunEventType.RUN_COMPLETED,
            {
                "state": stop.state.value,
                "reason": stop.reason.value,
                "result": {"verdict": stop.state.value, "explanation": stop.message},
            },
        )

    return RunResult(
        run_id=run.run_id,
        state=stop.state,
        reason=stop.reason,
        message=stop.message,
        total_ms=run.now_ms(),
        stages=tuple(run.stages),
        events=tuple(run.events),
        baseline_manifest_sha256=baseline.manifest_sha256 if baseline else None,
        baseline_outcome=baseline.execution.outcome.value if baseline else None,
        context_sha256=context_sha,
        batch_sha256=batch_identity(plan) if plan else None,
        selected_mutation_id=selection.target.mutation_id
        if selection and selection.target
        else None,
        plan=plan,
        first_pass=first_pass,
        selection=selection,
        candidate=candidate,
        verification=verification,
        rescore=rescore,
        observer_errors=run.observer_errors,
    )
