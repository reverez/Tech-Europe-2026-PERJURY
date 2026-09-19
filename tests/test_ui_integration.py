"""Frontend <-> real #17 API integration: real events/snapshots folded through the UI reducer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loop_fakes import FakeAnalyzer, FakeGenerator, ScriptedExecutor
from test_api import make_manager, read_sse, start, wait_terminal

from perjury.api import create_app
from perjury.contracts import ExecutionOutcome as O

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "perjury" / "ui"
NODE = shutil.which("node")


def capture(client: TestClient, tmp_path: Path, **expect: object) -> Path:
    run_id = start(client)
    events = read_sse(client, f"/api/runs/{run_id}/events")
    snapshot = wait_terminal(client, run_id)
    path = tmp_path / f"{run_id}.json"
    path.write_text(
        json.dumps(
            {
                "events": [e.model_dump(mode="json") for e in events],
                "snapshot": snapshot,
                "expect": expect or None,
            }
        )
    )
    return path


def contract_check(path: Path) -> None:
    done = subprocess.run(
        ["node", str(ROOT / "tests/ui/api_contract.mjs"), str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0 and "api contract ok" in done.stdout, done.stdout + done.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize(
    "name,manager_kwargs,expect",
    [
        (
            "verified",
            {},
            {"stage": "verified", "before": "62.5%", "after": "75%"},
        ),
        (
            "baseline_failed",
            {"executor": ScriptedExecutor(baseline=O.TEST_FAIL)},
            {"stage": "failed"},
        ),
        (
            "all_equivalent",
            {"analyzer": FakeAnalyzer(equivalent={"M01", "M02", "M07"})},
            {"stage": "inconclusive", "before": None},
        ),
        (
            "invalid_candidate",
            {"generator": FakeGenerator("def test_x(:\n    pass\n")},
            {"stage": "rejected"},
        ),
        (
            "undefined_after_score",
            {"executor": ScriptedExecutor(rescore={f"M0{i}": O.INFRA_ERROR for i in range(1, 9)})},
            {"stage": "inconclusive", "before": "62.5%", "after": "n/a"},
        ),
    ],
)
def test_real_api_output_folds_through_the_ui_reducer_to_the_snapshot(
    tmp_path, name, manager_kwargs, expect
) -> None:
    client = TestClient(create_app(make_manager(**manager_kwargs)))
    expect = {k: v for k, v in expect.items() if not (k == "before" and v is None)}
    contract_check(capture(client, tmp_path, **expect))


def test_ui_mount_does_not_shadow_the_backend_api() -> None:
    client = TestClient(create_app(make_manager()))
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/demo").status_code == 200
    assert client.get("/ui/app.js").status_code == 200
    run_id = start(client)
    assert client.post("/api/runs").status_code in {202, 409}  # API POST still routed to the API
    assert wait_terminal(client, run_id)["stage"] == "verified"
    assert client.get("/api/runs/unknown").json()["detail"]["code"] == "run_not_found"


def test_live_adapter_uses_only_the_real_api_and_never_mock_or_canned_data() -> None:
    src = (UI / "adapters.js").read_text()
    live = src[src.index("export function liveAdapter") : src.index("MOCK transport")]
    assert "mock" not in live.lower() and "canned" not in live.lower()
    assert "fetch('/api/runs', { method: 'POST' })" in live
    assert "/api/runs/${encodeURIComponent(id)}" in live and "new EventSource(" in live
    app = (UI / "app.js").read_text()
    assert "params.get('adapter') === 'mock'" in app and ": liveAdapter()" in app  # live is default


def test_simulated_banner_exists_and_is_only_shown_for_the_mock_adapter() -> None:
    html = (UI / "index.html").read_text()
    assert re.search(r'id="sim-banner"[^>]*hidden', html) and "SIMULATED" in html
    assert "toggleAttribute('hidden', adapter.kind !== 'mock')" in (UI / "app.js").read_text()


def test_ui_never_computes_scores_in_javascript() -> None:
    for name in ("render.js", "store.js", "app.js"):
        text = (UI / name).read_text()
        assert not re.search(r"killed\s*/\s*[(\w]\w*\s*[+)]|\.killed\s*/", text), name
        assert "killed +" not in text and "survived +" not in text, name
    render = (UI / "render.js").read_text()
    assert "deltaLabel(c)" in render and "s?.comparison" in render


def test_ui_supports_the_context_stage_and_does_not_wait_for_mutation_started() -> None:
    assert "['context', 'Context']" in (UI / "render.js").read_text()
    assert "case 'mutation.started'" not in (UI / "store.js").read_text()
    assert "mutation.started" not in (UI / "mock_events.js").read_text().replace(
        "no mutation.started", ""
    ).replace("NOT emitted", "")


def test_recovery_features_are_present_in_the_app() -> None:
    app = (UI / "app.js").read_text()
    assert "#run=" in app and "MAX_RESUBSCRIBES" in app and "snapshot(runId)" in app
    assert "?after=" in (UI / "adapters.js").read_text()  # resume cursor for SSE reconnect


def test_conditional_sections_are_never_stringified_into_the_page() -> None:
    render = (UI / "render.js").read_text()
    assert "kids.filter((k) => !!k)" in render  # null/false children are dropped by fill()
