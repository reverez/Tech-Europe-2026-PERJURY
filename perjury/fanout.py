"""Bounded concurrent mutation fan-out (#7).

Runs one isolated mutation workspace per job through a ``WorkspaceExecutor`` and returns
exactly one terminal result per requested mutation, in request order, regardless of completion
order. Sandbox cleanup is owned by the executor; workspace directory cleanup stays with the
caller that created the ``MutationWorkspace`` objects.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .contracts import (
    ExecutionOutcome,
    ExecutionResult,
    MutationStatus,
    SnapshotManifest,
    WorkspaceSpec,
)
from .workspace import WorkspaceExecutor

MAX_FANOUT_CONCURRENCY = 10
DEADLINE_GRACE_SECONDS = 45
POLL_INTERVAL_SECONDS = 0.05


class FanoutError(RuntimeError):
    """Raised for an invalid fan-out request (never for a per-mutation outcome)."""


@dataclass(frozen=True, slots=True)
class FanoutJob:
    """One isolated mutant workspace to execute."""

    mutation_id: str
    spec: WorkspaceSpec
    manifest: SnapshotManifest

    @property
    def execution_id(self) -> str:
        return f"mutation:{self.mutation_id}"


class FanoutJobResult(BaseModel):
    mutation_id: str
    execution: ExecutionResult
    started_offset_ms: int | None = Field(default=None, ge=0)
    finished_offset_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @property
    def status(self) -> MutationStatus:
        return self.execution.mutation_status


class FanoutResult(BaseModel):
    results: tuple[FanoutJobResult, ...]
    wall_clock_ms: int = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    peak_concurrency: int = Field(ge=0)
    cancelled: bool = False

    def by_mutation_id(self) -> dict[str, FanoutJobResult]:
        return {r.mutation_id: r for r in self.results}


def _job_deadline_seconds(spec: WorkspaceSpec) -> int:
    commands = 2 if spec.install_argv is not None else 1
    return spec.command_timeout_seconds * commands + DEADLINE_GRACE_SECONDS


def _synthetic(
    job: FanoutJob,
    outcome: ExecutionOutcome,
    detail: str,
    duration_ms: int = 0,
) -> ExecutionResult:
    return ExecutionResult(
        execution_id=job.execution_id,
        outcome=outcome,
        duration_ms=duration_ms,
        failure_detail=detail,
    )


def run_fanout(
    jobs: Sequence[FanoutJob],
    executor: WorkspaceExecutor,
    *,
    max_concurrency: int | None = None,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[FanoutJobResult], None] | None = None,
) -> FanoutResult:
    """Execute jobs with bounded concurrency and deterministic, complete collection.

    - Every job yields exactly one terminal result, ordered like ``jobs``.
    - An executor exception becomes INFRA_ERROR for that job only.
    - A job exceeding its hard deadline (command timeouts + grace) is recorded as TIMEOUT.
    - Setting ``cancel_event`` stops unstarted jobs (INFRA_ERROR, "cancelled"); running jobs
      finish through the executor, which terminates its own Sandbox.
    - An exception from ``on_result`` cancels the remaining work and is re-raised.
    """
    if not jobs:
        raise FanoutError("run_fanout requires at least one job")
    ids = [job.mutation_id for job in jobs]
    if len(set(ids)) != len(ids):
        raise FanoutError("mutation ids must be unique within one fan-out")

    limit = max_concurrency if max_concurrency is not None else jobs[0].spec.mutation_concurrency
    if not 1 <= limit <= MAX_FANOUT_CONCURRENCY:
        raise FanoutError(f"max_concurrency must be between 1 and {MAX_FANOUT_CONCURRENCY}")
    limit = min(limit, len(jobs))

    cancel = cancel_event or threading.Event()
    lock = threading.Lock()
    started_at: dict[str, float] = {}
    active = 0
    peak = 0
    t0 = time.perf_counter()

    def offset_ms(moment: float) -> int:
        return max(0, int((moment - t0) * 1000))

    def work(job: FanoutJob) -> ExecutionResult:
        nonlocal active, peak
        if cancel.is_set():
            return _synthetic(job, ExecutionOutcome.INFRA_ERROR, "cancelled before start")
        with lock:
            started_at[job.mutation_id] = time.perf_counter()
            active += 1
            peak = max(peak, active)
        try:
            result = executor.execute(
                job.spec, execution_id=job.execution_id, manifest=job.manifest
            )
            if result.execution_id != job.execution_id:
                return _synthetic(
                    job,
                    ExecutionOutcome.INFRA_ERROR,
                    f"executor returned execution_id={result.execution_id!r}",
                )
            return result
        except Exception as exc:  # noqa: BLE001 - one job's failure must not sink the batch
            return _synthetic(job, ExecutionOutcome.INFRA_ERROR, f"executor raised: {exc!r}")
        finally:
            with lock:
                active -= 1

    collected: dict[str, FanoutJobResult] = {}

    def record(job: FanoutJob, execution: ExecutionResult) -> None:
        if job.mutation_id in collected:
            return
        finished = time.perf_counter()
        began = started_at.get(job.mutation_id)
        item = FanoutJobResult(
            mutation_id=job.mutation_id,
            execution=execution,
            started_offset_ms=offset_ms(began) if began is not None else None,
            finished_offset_ms=offset_ms(finished),
            duration_ms=execution.duration_ms,
        )
        collected[job.mutation_id] = item
        if on_result is not None:
            on_result(item)

    pool = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="perjury-fanout")
    futures: dict[Future[ExecutionResult], FanoutJob] = {}
    try:
        for job in jobs:
            futures[pool.submit(work, job)] = job
        pending = set(futures)
        while pending:
            done, pending = wait(
                pending, timeout=POLL_INTERVAL_SECONDS, return_when=FIRST_COMPLETED
            )
            for future in done:
                record(futures[future], future.result())
            now = time.perf_counter()
            for future in list(pending):
                job = futures[future]
                began = started_at.get(job.mutation_id)
                if began is not None and now - began > _job_deadline_seconds(job.spec):
                    record(
                        job,
                        _synthetic(
                            job,
                            ExecutionOutcome.TIMEOUT,
                            f"hard fan-out deadline exceeded ({_job_deadline_seconds(job.spec)}s)",
                            int((now - began) * 1000),
                        ),
                    )
                    pending.discard(future)
    except BaseException:
        cancel.set()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    ordered = tuple(collected[job.mutation_id] for job in jobs)
    return FanoutResult(
        results=ordered,
        wall_clock_ms=int((time.perf_counter() - t0) * 1000),
        max_concurrency=limit,
        peak_concurrency=peak,
        cancelled=cancel.is_set(),
    )
