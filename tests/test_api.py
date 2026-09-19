"""Run API + SSE (#17) with deterministic injected model/execution boundaries (no credentials)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loop_fakes import FakeAnalyzer, FakeGenerator, ScriptedExecutor

from perjury.api import create_app
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.orchestrator import RunEvent, run_perjury
from perjury.runs import RunManager, project_snapshot
from perjury.workspace import refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = refund_workspace_spec(str(ROOT))

# Keys the frontend contract (perjury/ui/contract.js RunSnapshot) reads.
UI_SNAPSHOT_KEYS = {
    "run_id", "stage", "last_seq", "started_at_ms", "elapsed_ms", "commit_sha", "baseline",
    "mutations", "survivor_id", "analysis", "proposal", "verification", "result",
    "score_before", "score_after", "error",
}  # fmt: skip
UI_EVENT_TYPES = {
    "run.started", "baseline.completed", "plan.completed", "mutation.completed",
    "survivor.selected", "analysis.completed", "test.proposed", "verification.completed",
    "rescore.completed", "run.completed", "run.failed",
}  # fmt: skip


def make_manager(executor=None, *, gate: threading.Event | None = None, **model) -> RunManager:
    scripted = executor or ScriptedExecutor()
    if gate is not None:
        inner = scripted

        class Gated:
            def execute(self, spec, *, execution_id, manifest=None):
                if execution_id.startswith("baseline:"):
                    gate.wait(10)
                return inner.execute(spec, execution_id=execution_id, manifest=manifest)

        scripted = Gated()

    def execute(run_id, on_event):
        return run_perjury(
            SPEC,
            planner=model.get("planner") or CanonicalRefundPlanner(),
            analyzer=model.get("analyzer") or FakeAnalyzer(),
            generator=model.get("generator") or FakeGenerator(),
            executor=scripted,
            run_id=run_id,
            on_event=on_event,
            commit_sha="abc1234",
        )

    return RunManager(execute)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_manager()))


def start(client: TestClient) -> str:
    response = client.post("/api/runs")
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def wait_terminal(client: TestClient, run_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/runs/{run_id}").json()
        if snap["terminal"]:
            return snap
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def read_sse(client: TestClient, url: str, **kw) -> list[RunEvent]:
    events: list[RunEvent] = []
    with client.stream("GET", url, **kw) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        pending_id = None
        for line in response.iter_lines():
            if line.startswith("id: "):
                pending_id = int(line[4:])
            elif line.startswith("data: "):
                event = RunEvent.model_validate_json(line[6:])
                assert event.seq == pending_id  # SSE id matches the typed event seq
                events.append(event)
    return events


def test_health_and_root_are_unchanged(client) -> None:
    assert client.get("/health").json() == {"status": "ok", "service": "perjury"}
    assert client.get("/").json()["name"] == "PERJURY"


def test_post_starts_a_run_and_returns_typed_links(client) -> None:
    response = client.post("/api/runs")
    body = response.json()
    assert response.status_code == 202
    assert set(body) == {"run_id", "stage", "snapshot_url", "events_url"}
    assert body["snapshot_url"] == f"/api/runs/{body['run_id']}"
    assert body["events_url"] == f"/api/runs/{body['run_id']}/events"
    wait_terminal(client, body["run_id"])


def test_snapshot_matches_the_ui_contract_shapes_and_casing(client) -> None:
    snap = wait_terminal(client, start(client))
    assert UI_SNAPSHOT_KEYS <= set(snap)
    assert snap["stage"] == "verified" and snap["terminal"] is True and snap["error"] is None
    assert snap["commit_sha"] == "abc1234" and isinstance(snap["started_at_ms"], int)
    # outcome casing: semantic outcomes UPPERCASE, mutation statuses lowercase
    assert snap["baseline"]["outcome"] == "PASS"
    v = snap["verification"]
    assert (v["original"], v["mutant"]) == ("PASS", "TEST_FAIL")  # original/mutant, not *_outcome
    assert "original_outcome" not in v and "mutant_outcome" not in v
    assert {m["status"] for m in snap["mutations"]} == {"killed", "survived"}
    assert [m["id"] for m in snap["mutations"]] == [f"M0{i}" for i in range(1, 9)]
    assert all(m["diff"] and m["file_path"] for m in snap["mutations"])
    m01 = next(m for m in snap["mutations"] if m["id"] == "M01")
    assert m01["status"] == "survived"
    assert snap["survivor_id"] == "M01"
    assert snap["analysis"]["mutation_id"] == "M01" and snap["proposal"]["mutation_id"] == "M01"
    assert snap["result"]["verdict"] == "verified" and snap["result"]["reason"] == "verified"
    assert snap["candidate"]["candidate_path"] == "examples/refund/test_perjury_M01.py"


def test_score_and_comparison_are_authoritative_server_side(client) -> None:
    snap = wait_terminal(client, start(client))
    before, after, cmp_ = snap["score_before"], snap["score_after"], snap["comparison"]
    assert (before["killed"], before["survived"], before["excluded"]) == (5, 3, 0)
    assert (after["killed"], after["survived"], after["excluded"]) == (6, 2, 0)
    assert before["score"] == 5 / 8 and after["score"] == 6 / 8
    assert cmp_["delta"] == 6 / 8 - 5 / 8 and cmp_["direction"] == "improved"
    assert cmp_["status"] == "confirmed" and cmp_["newly_killed_ids"] == ["M01"]
    assert cmp_["batch_sha256"] == snap["batch_sha256"]
    assert snap["improvement_verified"] is True
    assert snap["stages"][0]["stage"] == "baseline" and snap["stages"][0]["evidence"]


def test_undefined_score_is_null_never_a_fabricated_zero() -> None:
    executor = ScriptedExecutor(
        rescore={f"M0{i}": O.INFRA_ERROR for i in range(1, 9)}
    )  # replay yields no valid outcome
    client = TestClient(create_app(make_manager(executor)))
    snap = wait_terminal(client, start(client))
    assert snap["stage"] == "inconclusive" and snap["reason"] == "rescore_inconsistent"
    assert snap["score_after"]["score"] is None and snap["score_after"]["state"] == "inconclusive"
    assert snap["comparison"]["delta"] is None and snap["comparison"]["direction"] == "unavailable"
    assert snap["score_before"]["score"] == 5 / 8


def test_events_stream_is_ordered_typed_and_correlated(client) -> None:
    run_id = start(client)
    events = read_sse(client, f"/api/runs/{run_id}/events")
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type.value == "run.started" and events[-1].type.value == "run.completed"
    assert all(e.run_id == run_id and e.correlation_id.startswith(run_id) for e in events)
    assert UI_EVENT_TYPES - {"run.failed"} <= {e.type.value for e in events}
    assert all(e.stage for e in events)
    mutation_events = [e for e in events if e.type.value == "mutation.completed"]
    assert len(mutation_events) == 8 and all(e.mutation_id for e in mutation_events)
    assert events[-1].data["result"]["verdict"] == "verified"
    wire = [e.model_dump(mode="json") for e in events]
    assert wire[0]["data"]["commit_sha"] == "abc1234" and json.dumps(wire)


def test_terminal_sse_event_agrees_with_the_authoritative_snapshot(client) -> None:
    run_id = start(client)
    events = read_sse(client, f"/api/runs/{run_id}/events")
    snap = client.get(f"/api/runs/{run_id}").json()  # fetched immediately after the terminal event
    terminal = events[-1]
    assert snap["terminal"] and snap["stage"] == terminal.stage.value == "verified"
    assert snap["result"]["verdict"] == terminal.data["result"]["verdict"]
    assert snap["result"]["explanation"] == terminal.data["result"]["explanation"]
    assert snap["last_seq"] == terminal.seq == len(events)
    assert snap["comparison"] is not None and snap["score_after"] is not None  # already final

    # the event-derived projection agrees with the authoritative snapshot on every UI field
    derived = project_snapshot(run_id, events).model_dump(mode="json")
    for key in (
        "stage", "baseline", "mutations", "survivor_id", "analysis", "proposal", "verification",
        "result", "score_before", "score_after", "error", "last_seq", "commit_sha", "batch_sha256",
    ):  # fmt: skip
        assert derived[key] == snap[key], key
    assert derived["comparison"] == snap["comparison"]


def test_reconnect_after_completion_replays_and_resumes(client) -> None:
    run_id = start(client)
    url = f"/api/runs/{run_id}/events"
    first = read_sse(client, url)
    again = read_sse(client, url)
    assert [e.model_dump() for e in again] == [e.model_dump() for e in first]
    resumed = read_sse(client, url, headers={"Last-Event-ID": "10"})
    assert [e.seq for e in resumed] == list(range(11, len(first) + 1))
    by_query = read_sse(client, f"{url}?after={len(first) - 1}")
    assert [e.seq for e in by_query] == [len(first)] and by_query[0].type.value == "run.completed"
    assert read_sse(client, url, headers={"Last-Event-ID": str(len(first))}) == []
    assert client.get(f"/api/runs/{run_id}").json()["stage"] == "verified"  # snapshot survives


def test_bad_last_event_id_is_a_typed_400(client) -> None:
    run_id = start(client)
    response = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "abc"})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "invalid_request"


def test_only_one_active_run_and_conflict_is_a_typed_409() -> None:
    gate = threading.Event()
    client = TestClient(create_app(make_manager(gate=gate)))
    first = start(client)
    try:
        conflict = client.post("/api/runs")
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["code"] == "run_active" and detail["active_run_id"] == first
        assert first in detail["message"]  # the UI surfaces detail.message
        # while active, the snapshot is non-terminal and not yet finalized
        live = client.get(f"/api/runs/{first}").json()
        assert live["terminal"] is False and live["result"] is None
        assert live["stage"] in {"created", "baseline"} and live["elapsed_ms"] is not None
    finally:
        gate.set()
    done = wait_terminal(client, first)
    assert done["stage"] == "verified"
    second = start(client)  # a new run is allowed once the first has finished
    assert second != first
    wait_terminal(client, second)
    assert client.get(f"/api/runs/{first}").json()["stage"] == "verified"  # both stay queryable


def test_concurrent_starts_admit_exactly_one_run() -> None:
    gate = threading.Event()
    client = TestClient(create_app(make_manager(gate=gate)))
    codes: list[int] = []

    def post() -> None:
        codes.append(client.post("/api/runs").status_code)

    threads = [threading.Thread(target=post) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gate.set()
    assert sorted(codes) == [202] + [409] * 5
    deadline = time.time() + 30
    while client.app.state.runs.active_run_id and time.time() < deadline:
        time.sleep(0.05)


def test_stream_can_be_consumed_while_the_run_is_in_flight() -> None:
    gate = threading.Event()
    client = TestClient(create_app(make_manager(gate=gate)))
    run_id = start(client)
    threading.Timer(0.3, gate.set).start()
    events = read_sse(client, f"/api/runs/{run_id}/events")  # opened before the run progressed
    assert events[-1].type.value == "run.completed"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_unknown_run_is_a_typed_404(client) -> None:
    for url in ("/api/runs/nope", "/api/runs/nope/events"):
        response = client.get(url)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "run_not_found"


def test_failed_run_snapshot_and_terminal_event_agree() -> None:
    client = TestClient(create_app(make_manager(ScriptedExecutor(baseline=O.TEST_FAIL))))
    run_id = start(client)
    events = read_sse(client, f"/api/runs/{run_id}/events")
    snap = client.get(f"/api/runs/{run_id}").json()
    assert events[-1].type.value == "run.failed" and events[-1].stage.value == "failed"
    assert snap["stage"] == "failed" and snap["result"] is None
    assert snap["error"]["code"] == events[-1].data["code"] == "baseline_not_ready"
    assert snap["error"]["message"] == events[-1].data["message"]
    assert snap["baseline"] is None and snap["score_before"] is None


@pytest.mark.parametrize(
    "kwargs,stage,reason",
    [
        ({"analyzer": FakeAnalyzer(equivalent={"M01", "M02", "M07"})}, "inconclusive",
         "all_possibly_equivalent"),
        ({"generator": FakeGenerator("def test_x(:\n    pass\n")}, "rejected", "candidate_invalid"),
    ],
)  # fmt: skip
def test_non_verified_terminal_states_use_result_not_error(kwargs, stage, reason) -> None:
    client = TestClient(create_app(make_manager(**kwargs)))
    snap = wait_terminal(client, start(client))
    assert snap["stage"] == stage and snap["error"] is None
    assert snap["result"]["verdict"] == stage and snap["result"]["reason"] == reason
    assert snap["score_after"] is None


def test_crashed_run_ends_with_typed_failure_and_frees_the_slot() -> None:
    def crash(run_id, on_event):
        raise RuntimeError("kaboom")

    client = TestClient(create_app(RunManager(crash)))
    run_id = start(client)
    events = read_sse(client, f"/api/runs/{run_id}/events")
    snap = client.get(f"/api/runs/{run_id}").json()
    assert events[-1].type.value == "run.failed"
    assert snap["stage"] == "failed" and snap["error"]["code"] == "internal_error"
    assert "kaboom" in snap["error"]["message"]
    assert start(client) != run_id  # slot released


def test_openapi_documents_typed_models(client) -> None:
    spec = client.get("/openapi.json").json()
    assert {"/api/runs", "/api/runs/{run_id}", "/api/runs/{run_id}/events", "/health"} <= set(
        spec["paths"]
    )
    schemas = spec["components"]["schemas"]
    assert {"RunSnapshot", "RunCreated", "ApiError"} <= set(schemas)
    assert "409" in spec["paths"]["/api/runs"]["post"]["responses"]


def test_importing_the_api_needs_no_credentials_and_builds_no_live_boundary() -> None:
    code = (
        "import os, sys\n"
        "os.environ.pop('GOOGLE_API_KEY', None)\n"
        "import perjury.api\n"
        "assert 'modal' not in sys.modules and 'perjury.agent' not in sys.modules\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stdout + done.stderr
