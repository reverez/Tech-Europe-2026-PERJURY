"""In-memory run registry and typed API views over the orchestrator (#17).

``RunManager`` runs the existing ``run_perjury`` path (injected as ``execute``) in a background
thread, keeps its ordered events, allows one active run at a time, and serves authoritative
snapshots. It contains no orchestration logic.

Authoritative-terminal rule: the terminal event (``run.completed`` / ``run.failed``) is withheld
from consumers until the final ``RunResult`` is stored, so a client that sees the terminal event and
immediately fetches the snapshot always gets the finalized, agreeing state.

View shapes follow the UI contract (``perjury/ui/contract.js`` on the frontend branch); see
``docs/API_CONTRACT.md`` for the mapping and the deliberate additions.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .execution import MutationScore
from .orchestrator import RunEvent, RunEventType, RunResult, RunStage, StageRecord
from .rescore import ScoreComparison

TERMINAL_VALUES = {"verified", "rejected", "inconclusive", "failed"}
TERMINAL_EVENTS = {RunEventType.RUN_COMPLETED, RunEventType.RUN_FAILED}
MAX_RETAINED_RUNS = 50

ExecuteFn = Callable[[str, Callable[[RunEvent], None]], RunResult]


class ApiError(BaseModel):
    """Typed error body, returned as ``{"detail": ApiError}`` (the UI reads ``detail.message``)."""

    code: Literal["run_active", "run_not_found", "invalid_request"]
    message: str
    active_run_id: str | None = None


class RunCreated(BaseModel):
    run_id: str
    stage: str
    snapshot_url: str
    events_url: str


class SnapshotScore(MutationScore):
    """UI score shape: killed/survived/excluded/score, but ``score`` is null (never 0%) when there
    is no valid denominator."""

    excluded: int = Field(ge=0)


class BaselineView(BaseModel):
    outcome: str  # PASS | TEST_FAIL | INVALID | TIMEOUT | INFRA_ERROR
    duration_ms: int
    summary: str = ""
    manifest_sha256: str | None = None


class MutationView(BaseModel):
    id: str
    file_path: str
    description: str
    hypothesis: str
    diff: str
    status: str = "pending"  # pending | killed | survived | invalid | timeout | infra_error
    duration_ms: int | None = None


class VerificationView(BaseModel):
    mutation_id: str
    original: str  # PASS | TEST_FAIL | INVALID | TIMEOUT | INFRA_ERROR
    mutant: str
    original_duration_ms: int
    mutant_duration_ms: int
    verdict: str
    explanation: str


class ResultView(BaseModel):
    verdict: Literal["verified", "rejected", "inconclusive"]
    explanation: str
    reason: str


class ErrorView(BaseModel):
    code: str
    message: str


class ComparisonView(BaseModel):
    status: str  # confirmed | inconsistent
    delta: float | None
    direction: str
    batch_sha256: str
    newly_killed_ids: list[str]
    newly_survived_ids: list[str]
    message: str


class CandidateView(BaseModel):
    mutation_id: str
    candidate_path: str
    sha256: str
    diff: str


class RunSnapshot(BaseModel):
    """Authoritative typed snapshot. Field names/casing match the UI contract."""

    run_id: str
    stage: str
    terminal: bool
    last_seq: int
    started_at_ms: int | None = None
    elapsed_ms: int | None = None
    commit_sha: str | None = None
    baseline: BaselineView | None = None
    mutations: list[MutationView] = Field(default_factory=list)
    survivor_id: str | None = None
    analysis: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    verification: VerificationView | None = None
    result: ResultView | None = None
    score_before: SnapshotScore | None = None
    score_after: SnapshotScore | None = None
    error: ErrorView | None = None
    # additions beyond the UI's current contract
    comparison: ComparisonView | None = None
    candidate: CandidateView | None = None
    improvement_verified: bool = False
    reason: str | None = None
    context_sha256: str | None = None
    batch_sha256: str | None = None
    stages: list[StageRecord] = Field(default_factory=list)


def _score(payload: dict[str, Any]) -> SnapshotScore:
    return SnapshotScore.model_validate(payload)


def project_snapshot(
    run_id: str,
    events: list[RunEvent],
    result: RunResult | None = None,
    *,
    elapsed_ms: int | None = None,
) -> RunSnapshot:
    """Fold ordered events into a snapshot (mirrors the UI reducer); a final result then
    supplies/overrides the authoritative fields."""
    snap = RunSnapshot(run_id=run_id, stage=RunStage.CREATED.value, terminal=False, last_seq=0)
    by_id: dict[str, MutationView] = {}
    for ev in events:
        if ev.run_id != run_id or ev.seq <= snap.last_seq:
            continue
        d = ev.data
        snap.last_seq = ev.seq
        snap.stage = ev.stage.value
        t = ev.type
        if t is RunEventType.RUN_STARTED:
            snap.started_at_ms = d.get("started_at_ms")
            snap.commit_sha = d.get("commit_sha")
        elif t is RunEventType.BASELINE_COMPLETED:
            snap.baseline = BaselineView.model_validate(d)
        elif t is RunEventType.CONTEXT_COMPLETED:
            snap.context_sha256 = d.get("context_sha256")
        elif t is RunEventType.PLAN_COMPLETED:
            snap.batch_sha256 = d.get("batch_sha256")
            by_id = {m["id"]: MutationView.model_validate(m) for m in d["mutations"]}
            snap.mutations = list(by_id.values())
        elif t is RunEventType.MUTATION_COMPLETED and ev.mutation_id in by_id:
            view = by_id[ev.mutation_id]
            view.status, view.duration_ms = d["status"], d.get("duration_ms")
        elif t is RunEventType.SURVIVOR_SELECTED:
            snap.survivor_id = ev.mutation_id
        elif t is RunEventType.ANALYSIS_COMPLETED:
            snap.analysis = d
        elif t is RunEventType.TEST_PROPOSED:
            snap.proposal = d
        elif t is RunEventType.CANDIDATE_READY and ev.mutation_id:
            snap.candidate = CandidateView(
                mutation_id=ev.mutation_id,
                candidate_path=d["candidate_path"],
                sha256=d["sha256"],
                diff=d["diff"],
            )
        elif t is RunEventType.VERIFICATION_COMPLETED and ev.mutation_id:
            snap.verification = VerificationView(mutation_id=ev.mutation_id, **d)
        elif t is RunEventType.RESCORE_COMPLETED:
            snap.score_before, snap.score_after = _score(d["before"]), _score(d["after"])
            snap.comparison = ComparisonView(
                status=d["status"],
                delta=d["delta"],
                direction=d["direction"],
                batch_sha256=d["batch_sha256"],
                newly_killed_ids=list(d.get("newly_killed_ids", [])),
                newly_survived_ids=list(d.get("newly_survived_ids", [])),
                message=d["message"],
            )
        elif t is RunEventType.RUN_COMPLETED:
            snap.result = ResultView(
                verdict=d["result"]["verdict"],
                explanation=d["result"]["explanation"],
                reason=d["reason"],
            )
            snap.reason = d["reason"]
        elif t is RunEventType.RUN_FAILED:
            snap.error = ErrorView(code=d["code"], message=d["message"])
            snap.reason = d["code"]
        snap.terminal = snap.stage in TERMINAL_VALUES

    if elapsed_ms is not None:
        snap.elapsed_ms = elapsed_ms
    if result is not None:
        _apply_result(snap, result)
    return snap


def _apply_result(snap: RunSnapshot, result: RunResult) -> None:
    """Overlay the authoritative orchestrator result on the event-derived snapshot."""
    snap.stage = result.state.value
    snap.terminal = True
    snap.elapsed_ms = result.total_ms
    snap.reason = result.reason.value
    snap.stages = list(result.stages)
    snap.context_sha256 = result.context_sha256
    snap.batch_sha256 = result.batch_sha256
    snap.improvement_verified = result.improvement_verified
    if result.state is RunStage.FAILED:
        snap.error = ErrorView(code=result.reason.value, message=result.message)
        snap.result = None
    else:
        snap.error = None
        snap.result = ResultView(
            verdict=result.state.value,  # type: ignore[arg-type]
            explanation=result.message,
            reason=result.reason.value,
        )
    if result.rescore is not None:
        c: ScoreComparison = result.rescore.comparison
        snap.score_before = _score({**c.before.model_dump(), "excluded": c.before.excluded_total})
        snap.score_after = _score({**c.after.model_dump(), "excluded": c.after.excluded_total})
        snap.comparison = ComparisonView(
            status=result.rescore.status.value,
            delta=c.delta,
            direction=c.direction,
            batch_sha256=c.batch_sha256,
            newly_killed_ids=list(c.newly_killed_ids),
            newly_survived_ids=list(c.newly_survived_ids),
            message=result.rescore.message,
        )


class RunRecord:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.lock = threading.Lock()
        self.events: list[RunEvent] = []
        self.result: RunResult | None = None
        self.crash: tuple[str, str] | None = None
        self.done = False
        self.created = time.time()
        self._held_terminal: RunEvent | None = None

    def publish(self, event: RunEvent) -> None:
        """Called from the run thread. The terminal event is held until the result is stored."""
        with self.lock:
            if event.type in TERMINAL_EVENTS:
                self._held_terminal = event
            else:
                self.events.append(event)

    def finish(self, result: RunResult | None, crash: tuple[str, str] | None = None) -> None:
        with self.lock:
            self.result, self.crash = result, crash
            if result is not None:
                if self._held_terminal is not None:
                    self.events.append(self._held_terminal)
            else:  # crashed: synthesize the terminal event so streams always end cleanly
                code, message = crash or ("internal_error", "run ended without a result")
                last = self.events[-1] if self.events else None
                self.events.append(
                    RunEvent(
                        run_id=self.run_id,
                        seq=(last.seq + 1) if last else 1,
                        type=RunEventType.RUN_FAILED,
                        stage=RunStage.FAILED,
                        at_ms=last.at_ms if last else 0,
                        correlation_id=f"{self.run_id}:failed",
                        data={"code": code, "message": message},
                    )
                )
            self.done = True

    def events_after(self, seq: int) -> tuple[list[RunEvent], bool]:
        """Events with ``seq`` greater than the cursor, and whether the stream is complete."""
        with self.lock:
            fresh = [e for e in self.events if e.seq > seq]
            # once done, no event is ever added again, so `fresh` is everything that remains
            return fresh, self.done

    def snapshot(self, wall_ms: Callable[[], int]) -> RunSnapshot:
        with self.lock:
            events = list(self.events)
            result, crash, done = self.result, self.crash, self.done
        snap = project_snapshot(self.run_id, events, result)
        if result is None and not done:
            started = snap.started_at_ms
            snap.elapsed_ms = max(0, wall_ms() - started) if started is not None else None
        if crash is not None:
            snap.error = ErrorView(code=crash[0], message=crash[1])
            snap.stage, snap.terminal, snap.reason = RunStage.FAILED.value, True, crash[0]
        return snap


class RunConflictError(Exception):
    def __init__(self, active_run_id: str) -> None:
        self.active_run_id = active_run_id
        super().__init__(f"run {active_run_id} is already active")


class RunManager:
    """One active run at a time; runs stay queryable after completion (bounded retention)."""

    def __init__(
        self,
        execute: ExecuteFn,
        *,
        id_factory: Callable[[], str] = lambda: f"run-{uuid.uuid4().hex[:12]}",
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._execute = execute
        self._id_factory = id_factory
        self.wall_ms = wall_ms
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._active: str | None = None

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active

    def start(self) -> RunRecord:
        with self._lock:
            if self._active is not None:
                raise RunConflictError(self._active)
            record = RunRecord(self._id_factory())
            self._runs[record.run_id] = record
            self._active = record.run_id
            while len(self._runs) > MAX_RETAINED_RUNS:
                oldest = next(iter(self._runs))
                if oldest == self._active:
                    break
                del self._runs[oldest]
        threading.Thread(
            target=self._run, args=(record,), name=f"perjury-{record.run_id}", daemon=True
        ).start()
        return record

    def _run(self, record: RunRecord) -> None:
        result: RunResult | None = None
        crash: tuple[str, str] | None = None
        try:
            result = self._execute(record.run_id, record.publish)
        except Exception as exc:  # noqa: BLE001 - a crashed run must still terminate cleanly
            crash = ("internal_error", f"{type(exc).__name__}: {exc}")
        finally:
            record.finish(result, crash)
            with self._lock:
                if self._active == record.run_id:
                    self._active = None

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)
