"""FastAPI surface: health + typed run API and SSE event stream (#17).

The API only starts, observes and serves the existing ``run_perjury`` orchestration through
``RunManager``; it contains no orchestration logic. Live model/execution boundaries are built
lazily when a run starts, so importing this module needs no credentials and touches no network.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .evidence import EvidenceStore
from .orchestrator import RunEvent, RunResult
from .runs import ApiError, RunConflictError, RunCreated, RunManager, RunRecord, RunSnapshot
from .ui import mount_ui

POLL_SECONDS = 0.05
KEEPALIVE_SECONDS = 15.0
ROOT = Path(__file__).resolve().parents[1]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _commit_sha(cwd: Path = ROOT) -> str | None:
    """Short HEAD SHA, suffixed ``-dirty`` when tracked files differ from it, so evidence never
    implies a clean commit that was not actually run."""
    head = _git(["rev-parse", "--short", "HEAD"], cwd)
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return None
    sha = head.stdout.strip()
    status = _git(["status", "--porcelain", "--untracked-files=no"], cwd)
    dirty = status is None or status.returncode != 0 or bool(status.stdout.strip())
    return f"{sha}-dirty" if dirty else sha


def live_execute(run_id: str, on_event: Callable[[RunEvent], None]) -> RunResult:
    """Default production boundary: Gemini planner/analyzer/generator + Modal execution.

    Everything external is imported and constructed here, at run start, never at import time.
    """
    from .analysis import GeminiSurvivorAnalyzer
    from .generation import GeminiTestGenerator
    from .modal_runner import ModalWorkspaceExecutor
    from .orchestrator import RunConfig, run_perjury
    from .planning import GeminiMutationPlanner, PlanningConfig
    from .workspace import LocalPytestExecutor, refund_workspace_spec

    return run_perjury(
        refund_workspace_spec(str(ROOT)),
        planner=GeminiMutationPlanner(),
        analyzer=GeminiSurvivorAnalyzer(),
        generator=GeminiTestGenerator(),
        executor=ModalWorkspaceExecutor(),
        baseline_executor=LocalPytestExecutor(),
        config=RunConfig(planning=PlanningConfig.from_env()),
        run_id=run_id,
        on_event=on_event,
        commit_sha=_commit_sha(),
    )


def default_evidence_store() -> EvidenceStore:
    """Evidence for the demo workspace goes to the gitignored ``.perjury/`` directory."""
    from .workspace import refund_workspace_spec

    return EvidenceStore(ROOT / ".perjury", refund_workspace_spec(str(ROOT)), _commit_sha)


def _error(status: int, code: str, message: str, active_run_id: str | None = None) -> HTTPException:
    body = ApiError(code=code, message=message, active_run_id=active_run_id)  # type: ignore[arg-type]
    return HTTPException(status_code=status, detail=body.model_dump(exclude_none=True))


def _record(manager: RunManager, run_id: str) -> RunRecord:
    record = manager.get(run_id)
    if record is None:
        raise _error(404, "run_not_found", f"no run with id {run_id!r}")
    return record


def _sse(event: RunEvent) -> str:
    return f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"


async def _event_stream(record: RunRecord, request: Request, after: int) -> AsyncIterator[str]:
    cursor, idle = after, 0.0
    while True:
        fresh, complete = record.events_after(cursor)
        for event in fresh:
            yield _sse(event)
            cursor, idle = event.seq, 0.0
        if complete:
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(POLL_SECONDS)
        idle += POLL_SECONDS
        if idle >= KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            idle = 0.0


def create_app(manager: RunManager | None = None) -> FastAPI:
    """Build the app. Tests inject a manager with deterministic boundaries."""
    manager = manager or RunManager(live_execute, evidence=default_evidence_store())
    api = FastAPI(
        title="PERJURY",
        description="Autonomous adversarial test-hardening agent",
        version="0.1.0",
    )
    api.state.runs = manager

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "perjury"}

    @api.get("/")
    def root() -> dict[str, str]:
        return {
            "name": "PERJURY",
            "tagline": "Your CI is green. PERJURY finds what your tests never proved.",
        }

    @api.post(
        "/api/runs",
        response_model=RunCreated,
        status_code=202,
        responses={409: {"model": ApiError}},
    )
    def start_run() -> RunCreated:
        try:
            record = manager.start()
        except RunConflictError as exc:
            raise _error(
                409,
                "run_active",
                f"Run {exc.active_run_id} is still active; only one run may be active.",
                exc.active_run_id,
            ) from exc
        return RunCreated(
            run_id=record.run_id,
            stage="created",
            snapshot_url=f"/api/runs/{record.run_id}",
            events_url=f"/api/runs/{record.run_id}/events",
        )

    @api.get(
        "/api/runs/{run_id}",
        response_model=RunSnapshot,
        responses={404: {"model": ApiError}},
    )
    def get_run(run_id: str) -> RunSnapshot:
        return _record(manager, run_id).snapshot(manager.wall_ms)

    @api.get(
        "/api/runs/{run_id}/evidence",
        responses={404: {"model": ApiError}},
    )
    def get_evidence(run_id: str) -> JSONResponse:
        """The sanitized evidence bundle (same redaction policy as the persisted file)."""
        _record(manager, run_id)
        bundle = manager.evidence.load(run_id) if manager.evidence is not None else None
        if bundle is None:
            raise _error(404, "evidence_unavailable", f"no evidence bundle for run {run_id!r}")
        return JSONResponse(bundle)

    @api.get("/api/runs/{run_id}/events", responses={404: {"model": ApiError}})
    async def run_events(
        run_id: str,
        request: Request,
        after: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        record = _record(manager, run_id)
        cursor = after if after is not None else 0
        if after is None and last_event_id:
            if not last_event_id.isdigit():
                raise _error(400, "invalid_request", "Last-Event-ID must be a non-negative integer")
            cursor = int(last_event_id)
        return StreamingResponse(
            _event_stream(record, request, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    mount_ui(api)  # zero-build demo UI at /demo and /ui (#18); API routes above stay authoritative
    return api


app = create_app()
