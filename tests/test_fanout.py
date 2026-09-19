from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from perjury import fanout
from perjury.contracts import (
    ExecutionOutcome,
    ExecutionResult,
    MutationStatus,
    SnapshotManifest,
    WorkspaceSpec,
)
from perjury.fanout import FanoutError, FanoutJob, run_fanout
from perjury.workspace import build_snapshot_manifest


@pytest.fixture
def base(tmp_path: Path) -> tuple[WorkspaceSpec, SnapshotManifest]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("X = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_a(): pass\n")
    spec = WorkspaceSpec(
        workspace_id="fanout-unit",
        source_root=str(tmp_path),
        snapshot_id="fixture:fanout",
        mutable_paths=("src",),
        context_paths=("tests",),
    )
    return spec, build_snapshot_manifest(spec)


def make_jobs(base: tuple[WorkspaceSpec, SnapshotManifest], n: int) -> list[FanoutJob]:
    spec, manifest = base
    return [FanoutJob(f"M{i:02d}", spec, manifest) for i in range(1, n + 1)]


def ok(execution_id: str, code: int = 1) -> ExecutionResult:
    outcome = ExecutionOutcome.PASS if code == 0 else ExecutionOutcome.TEST_FAIL
    return ExecutionResult(
        execution_id=execution_id, outcome=outcome, exit_code=code, duration_ms=5
    )


class ScriptedExecutor:
    def __init__(self, behaviour=None, delay: float = 0.0) -> None:
        self.behaviour = behaviour or (lambda eid: ok(eid))
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.seen: list[str] = []

    def execute(self, spec, *, execution_id, manifest=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.seen.append(execution_id)
        try:
            time.sleep(self.delay)
            return self.behaviour(execution_id)
        finally:
            with self.lock:
                self.active -= 1


def test_ten_jobs_run_concurrently_and_yield_one_result_each(base) -> None:
    executor = ScriptedExecutor(delay=0.2)
    result = run_fanout(make_jobs(base, 10), executor, max_concurrency=10)
    assert len(result.results) == 10
    assert executor.peak == 10
    assert result.peak_concurrency == 10
    assert result.wall_clock_ms < 1500  # serial would be ~2000ms
    assert sorted(executor.seen) == [f"mutation:M{i:02d}" for i in range(1, 11)]


def test_concurrency_bound_is_respected(base) -> None:
    executor = ScriptedExecutor(delay=0.05)
    result = run_fanout(make_jobs(base, 8), executor, max_concurrency=3)
    assert executor.peak <= 3
    assert result.max_concurrency == 3
    assert len(result.results) == 8


def test_default_concurrency_comes_from_workspace_spec(base) -> None:
    spec, manifest = base
    spec = spec.model_copy(update={"mutation_concurrency": 2})
    executor = ScriptedExecutor(delay=0.05)
    run_fanout([FanoutJob(f"M{i:02d}", spec, manifest) for i in range(1, 6)], executor)
    assert executor.peak <= 2


def test_results_keep_request_order_and_identity_despite_completion_order(base) -> None:
    def behaviour(execution_id: str) -> ExecutionResult:
        # earlier ids finish LAST
        index = int(execution_id[-2:])
        time.sleep((6 - index) * 0.05)
        return ok(execution_id, code=0 if index == 3 else 1)

    result = run_fanout(make_jobs(base, 5), ScriptedExecutor(behaviour), max_concurrency=5)
    assert [r.mutation_id for r in result.results] == [f"M0{i}" for i in range(1, 6)]
    by_id = result.by_mutation_id()
    assert by_id["M03"].status is MutationStatus.SURVIVED
    assert by_id["M01"].status is MutationStatus.KILLED
    assert all(r.execution.execution_id == f"mutation:{r.mutation_id}" for r in result.results)


def test_executor_exception_becomes_infra_error_without_sinking_batch(base) -> None:
    def behaviour(execution_id: str) -> ExecutionResult:
        if execution_id.endswith("02"):
            raise RuntimeError("boom")
        return ok(execution_id)

    result = run_fanout(make_jobs(base, 3), ScriptedExecutor(behaviour))
    statuses = [r.execution.outcome for r in result.results]
    assert statuses == [
        ExecutionOutcome.TEST_FAIL,
        ExecutionOutcome.INFRA_ERROR,
        ExecutionOutcome.TEST_FAIL,
    ]
    assert "boom" in (result.results[1].execution.failure_detail or "")


def test_executor_timeout_outcome_is_preserved_explicitly(base) -> None:
    def behaviour(execution_id: str) -> ExecutionResult:
        return ExecutionResult(
            execution_id=execution_id,
            outcome=ExecutionOutcome.TIMEOUT,
            duration_ms=9,
            failure_detail="slow",
        )

    result = run_fanout(make_jobs(base, 2), ScriptedExecutor(behaviour))
    assert {r.status for r in result.results} == {MutationStatus.TIMEOUT}


def test_hard_deadline_records_timeout_for_hung_job(base, monkeypatch) -> None:
    monkeypatch.setattr(fanout, "_job_deadline_seconds", lambda spec: 0.2)
    release = threading.Event()

    def behaviour(execution_id: str) -> ExecutionResult:
        if execution_id.endswith("01"):
            release.wait(5)
        return ok(execution_id)

    started = time.perf_counter()
    try:
        result = run_fanout(make_jobs(base, 3), ScriptedExecutor(behaviour), max_concurrency=3)
    finally:
        release.set()
    assert time.perf_counter() - started < 3
    assert result.results[0].execution.outcome is ExecutionOutcome.TIMEOUT
    assert result.results[1].execution.outcome is ExecutionOutcome.TEST_FAIL
    assert len(result.results) == 3


def test_wrong_execution_id_from_executor_is_infra_error(base) -> None:
    result = run_fanout(make_jobs(base, 1), ScriptedExecutor(lambda _eid: ok("mutation:M99")))
    assert result.results[0].execution.outcome is ExecutionOutcome.INFRA_ERROR


def test_durations_and_offsets_are_recorded(base) -> None:
    result = run_fanout(make_jobs(base, 4), ScriptedExecutor(delay=0.05), max_concurrency=2)
    assert result.wall_clock_ms >= 100
    for item in result.results:
        assert item.duration_ms == 5
        assert item.started_offset_ms is not None
        assert item.finished_offset_ms >= item.started_offset_ms


def test_pre_set_cancel_yields_terminal_result_for_every_job(base) -> None:
    cancel = threading.Event()
    cancel.set()
    executor = ScriptedExecutor()
    result = run_fanout(make_jobs(base, 4), executor, cancel_event=cancel)
    assert executor.seen == []
    assert result.cancelled
    assert len(result.results) == 4
    assert all(r.execution.outcome is ExecutionOutcome.INFRA_ERROR for r in result.results)
    assert all("cancelled" in (r.execution.failure_detail or "") for r in result.results)


def test_on_result_failure_cancels_remaining_and_reraises(base) -> None:
    executor = ScriptedExecutor(delay=0.05)
    cancel = threading.Event()

    def observer(_item) -> None:
        raise RuntimeError("observer fatal")

    with pytest.raises(RuntimeError, match="observer fatal"):
        run_fanout(
            make_jobs(base, 6), executor, max_concurrency=1, cancel_event=cancel, on_result=observer
        )
    assert cancel.is_set()
    time.sleep(0.4)
    assert len(executor.seen) < 6  # unstarted jobs were skipped


def test_on_result_receives_each_result_once(base) -> None:
    seen: list[str] = []
    run_fanout(
        make_jobs(base, 5), ScriptedExecutor(), on_result=lambda r: seen.append(r.mutation_id)
    )
    assert sorted(seen) == [f"M0{i}" for i in range(1, 6)]


def test_invalid_requests_are_rejected(base) -> None:
    executor = ScriptedExecutor()
    with pytest.raises(FanoutError, match="at least one"):
        run_fanout([], executor)
    jobs = make_jobs(base, 2)
    with pytest.raises(FanoutError, match="unique"):
        run_fanout([jobs[0], jobs[0]], executor)
    with pytest.raises(FanoutError, match="between 1 and"):
        run_fanout(jobs, executor, max_concurrency=11)
    with pytest.raises(FanoutError, match="between 1 and"):
        run_fanout(jobs, executor, max_concurrency=0)
