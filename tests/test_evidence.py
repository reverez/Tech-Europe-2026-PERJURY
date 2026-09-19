from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loop_fakes import FakeAnalyzer, FakeGenerator, ScriptedExecutor
from test_api import make_manager, read_sse, start, wait_terminal

from perjury import evidence as ev
from perjury.api import create_app
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import ExecutionResult
from perjury.evidence import (
    REDACTED,
    EvidenceStore,
    build_evidence_bundle,
    canonical_json,
    sanitize,
    verify_bundle_hash,
    write_json_atomic,
)
from perjury.orchestrator import run_perjury
from perjury.runs import RunManager
from perjury.workspace import refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = refund_workspace_spec(str(ROOT))
GEMINI_KEY = "AIza" + "a" * 35
SK_KEY = "sk-" + "b" * 30
ENV = {"GOOGLE_API_KEY": GEMINI_KEY, "MODAL_TOKEN_SECRET": "modal-secret-value-123", "HOME": "/x"}


def run(executor=None, **model):
    return run_perjury(
        SPEC,
        planner=CanonicalRefundPlanner(),
        analyzer=model.get("analyzer") or FakeAnalyzer(),
        generator=model.get("generator") or FakeGenerator(),
        executor=executor or ScriptedExecutor(),
        run_id="run-evidence",
        commit_sha="abc1234",
    )


@pytest.fixture(scope="module")
def result():
    return run()


def bundle_for(result, **kw):
    return build_evidence_bundle(
        run_id="run-evidence",
        spec=SPEC,
        commit_sha="abc1234",
        result=result,
        env=kw.pop("env", {}),
        written_at_ms=1_700_000_000_000,
        **kw,
    )


def resolve(bundle, pointer: str):
    node = bundle
    for part in pointer.split("."):
        node = node[part]
    return node


# ------------------------------------------------------------------ contents


def test_bundle_carries_identity_timings_and_all_required_sections(result) -> None:
    b = bundle_for(result)
    assert b["schema_version"] == "perjury.evidence.v1"
    assert b["run_id"] == "run-evidence" and b["commit_sha"] == "abc1234"
    assert b["outcome"]["state"] == "verified" and b["outcome"]["improvement_verified"] is True
    run_ = b["run"]
    assert run_["stages"][0]["stage"] == "baseline"  # stage timings
    assert all("duration_ms" in s and s["evidence"] for s in run_["stages"])
    assert len(run_["plan"]["batch"]["mutations"]) == 8  # model proposals
    assert run_["selection"]["analyses"][0]["mutation_id"] == "M01"
    trials = run_["first_pass"]["trials"]  # mutation diffs + semantic outcomes
    assert len(trials) == 8 and all(t["diff"] and t["execution"]["outcome"] for t in trials)
    assert all("stdout_truncated" in t["execution"] for t in trials)
    assert run_["candidate"]["plan"]["candidate_path"] == "examples/refund/test_perjury_M01.py"
    assert run_["candidate"]["plan"]["code"].startswith("from examples")
    v = run_["verification"]["result"]["evidence"]
    assert (v["original_outcome"], v["mutant_outcome"]) == ("pass", "test_fail")
    c = run_["rescore"]["comparison"]  # authoritative before/after
    assert (c["before"]["score"], c["after"]["score"], c["delta"]) == (0.625, 0.75, 0.125)
    assert b["complete"] is True and b["redactions"] == [] and b["truncations"] == []


def test_every_run_and_mutation_has_correlation_identity(result) -> None:
    events = bundle_for(result)["run"]["events"]
    assert all(
        e["run_id"] == "run-evidence" and e["correlation_id"].startswith("run-evidence")
        for e in events
    )
    per_mutation = [e for e in events if e["type"] == "mutation.completed"]
    assert len(per_mutation) == 8
    assert all(
        e["mutation_id"] and e["correlation_id"].endswith(e["mutation_id"]) for e in per_mutation
    )


def test_provenance_separates_proposal_fact_and_metric_and_pointers_resolve(result) -> None:
    b = bundle_for(result)
    assert set(b["provenance"]) == {"model_proposal", "executed_fact", "derived_metric"}
    for pointers in b["provenance"].values():
        for pointer in pointers:
            assert resolve(b, pointer) is not None, pointer
    all_pointers = [p for ps in b["provenance"].values() for p in ps]
    assert len(all_pointers) == len(set(all_pointers))  # each statement has exactly one kind


def test_workspace_view_never_exposes_environment_values_or_host_paths(result) -> None:
    w = bundle_for(result)["workspace"]
    assert "environment" not in w and w["environment_keys"] == ["PYTHONDONTWRITEBYTECODE"]
    assert w["source_root"] == ROOT.name and str(ROOT) not in canonical_json(bundle_for(result))


# ------------------------------------------------------------------ determinism


def test_serialization_is_deterministic_sorted_and_hash_checked(result) -> None:
    first, second = canonical_json(bundle_for(result)), canonical_json(bundle_for(result))
    assert first == second
    parsed = json.loads(first)
    assert list(parsed) == sorted(parsed)  # sorted keys
    assert verify_bundle_hash(parsed)
    parsed["outcome"]["state"] = "verified-ish"
    assert not verify_bundle_hash(parsed)  # tampering is detectable
    assert json.loads(first)["bundle_sha256"].startswith("sha256:")


# ------------------------------------------------------------------ redaction


def secret_result():
    class Leaky(ScriptedExecutor):
        def execute(self, spec, *, execution_id, manifest=None):
            base = super().execute(spec, execution_id=execution_id, manifest=manifest)
            if execution_id == "mutation:M03":
                leak = f"GOOGLE_API_KEY={GEMINI_KEY}\nAuthorization: Bearer {'z' * 24}\n{SK_KEY}"
                return base.model_copy(update={"stdout": leak, "stderr": "modal-secret-value-123"})
            return base

    return run(Leaky())


def test_secrets_are_redacted_before_persistence_and_reported() -> None:
    b = build_evidence_bundle(
        run_id="run-evidence", spec=SPEC, commit_sha="abc", result=secret_result(), env=ENV,
        written_at_ms=1,
    )  # fmt: skip
    text = canonical_json(b)
    for secret in (GEMINI_KEY, SK_KEY, "z" * 24, "modal-secret-value-123"):
        assert secret not in text, secret
    assert REDACTED in text
    kinds = {r["kind"] for r in b["redactions"]}
    assert {"secret_pattern", "environment_secret"} <= kinds
    assert all(r["path"].startswith("run.") for r in b["redactions"])
    assert any("stdout" in r["path"] for r in b["redactions"])
    assert b["complete"] is False  # evidence does not imply completeness once redacted


def test_environment_is_never_serialized_wholesale() -> None:
    env = {"GOOGLE_API_KEY": GEMINI_KEY, "SOME_PATH": "/home/user/private", "LANG": "C.UTF-8"}
    text = canonical_json(bundle_for(run(), env=env))
    assert "SOME_PATH" not in text and "/home/user/private" not in text and "C.UTF-8" not in text


@pytest.mark.parametrize(
    "value",
    [
        f"key {GEMINI_KEY} end",
        f"token {SK_KEY}",
        "ghp_" + "c" * 36,
        "xoxb-1234567890-abcdef",
        "AKIA" + "D" * 16,
        "Bearer " + "e" * 20,
        'api_key = "supersecretvalue"',
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_secret_patterns_are_redacted_in_any_string(value: str) -> None:
    clean, report = sanitize({"a": {"b": [value]}})
    assert clean["a"]["b"][0] != value and REDACTED in clean["a"]["b"][0]
    assert report.redactions == [{"path": "a.b[0]", "kind": "secret_pattern"}]


def test_secret_looking_keys_are_redacted_but_ordinary_text_is_kept() -> None:
    clean, report = sanitize(
        {
            "api_key": "abc12345",
            "auth_token": "x" * 10,
            "password": 1234,
            "note": "the token bucket",
        }
    )
    assert clean["api_key"] == clean["auth_token"] == clean["password"] == REDACTED
    assert clean["note"] == "the token bucket"  # only key names/patterns trigger redaction
    assert {r["kind"] for r in report.redactions} == {"secret_key"}


def test_short_or_absent_env_secrets_do_not_over_redact() -> None:
    assert ev.secret_values_from_env({"MY_TOKEN": "short", "PATHX": "long-value-here"}) == []
    clean, report = sanitize("the value abc", secrets=[])
    assert clean == "the value abc" and report.redactions == []


# ------------------------------------------------------------------ bounds / truncation


def test_oversized_strings_are_truncated_and_marked() -> None:
    big = "x" * (ev.MAX_STRING_CHARS + 500)
    clean, report = sanitize({"stdout": big})
    assert len(clean["stdout"]) < len(big) and "…[truncated 500 chars]" in clean["stdout"]
    assert report.truncations == [{"path": "stdout", "original_chars": len(big)}]


def test_output_bound_metadata_survives_serialization() -> None:
    class Truncating(ScriptedExecutor):
        def execute(self, spec, *, execution_id, manifest=None):
            base = super().execute(spec, execution_id=execution_id, manifest=manifest)
            if execution_id == "mutation:M04":
                return ExecutionResult.model_validate(
                    {**base.model_dump(), "stdout": "y" * 1000, "stdout_truncated": True,
                     "stderr_truncated": True}
                )  # fmt: skip
            return base

    b = bundle_for(run(Truncating()))
    trial = next(t for t in b["run"]["first_pass"]["trials"] if t["mutation_id"] == "M04")
    assert trial["execution"]["stdout_truncated"] is True
    assert trial["execution"]["stderr_truncated"] is True


def test_report_entries_are_bounded_and_the_overflow_is_counted() -> None:
    _clean, report = sanitize([f"AIza{'q' * 35}" for _ in range(ev.MAX_REPORT_ENTRIES + 20)])
    assert len(report.redactions) == ev.MAX_REPORT_ENTRIES and report.dropped_report_entries == 20


# ------------------------------------------------------------------ failed / inconclusive / crashed


def test_failed_run_retains_useful_evidence() -> None:
    failed = run(ScriptedExecutor(baseline=O.TEST_FAIL))
    b = bundle_for(failed)
    assert b["outcome"]["state"] == "failed" and b["outcome"]["reason"] == "baseline_not_ready"
    assert [s["stage"] for s in b["run"]["stages"]] == ["baseline"]
    assert b["run"]["events"][-1]["type"] == "run.failed"


def test_inconsistent_run_keeps_verification_and_rescore_evidence() -> None:
    executor = ScriptedExecutor(rescore=dict(ScriptedExecutor().first_pass))  # M01 still survives
    b = bundle_for(run(executor))
    assert (
        b["outcome"]["state"] == "inconclusive" and b["outcome"]["reason"] == "rescore_inconsistent"
    )
    assert b["run"]["verification"]["result"]["verdict"] == "verified"
    assert (
        b["run"]["rescore"]["status"] == "inconsistent" and b["run"]["rescore"]["inconsistencies"]
    )


def test_crashed_run_bundle_keeps_events_and_the_crash() -> None:
    events = run().events[:5]
    b = build_evidence_bundle(
        run_id="run-evidence", spec=SPEC, commit_sha="abc", result=None, events=list(events),
        crash=("internal_error", "RuntimeError: boom"), env={}, written_at_ms=1,
    )  # fmt: skip
    assert b["run"] is None and b["crash"] == {
        "code": "internal_error",
        "message": "RuntimeError: boom",
    }
    assert len(b["events"]) == 5 and b["outcome"]["state"] == "failed"


# ------------------------------------------------------------------ atomic persistence


def test_store_writes_atomically_under_the_run_directory(tmp_path, result) -> None:
    store = EvidenceStore(tmp_path / ".perjury", SPEC, "abc1234", env={})
    ref = store.save("run-evidence", result)
    path = tmp_path / ".perjury" / "runs" / "run-evidence" / "evidence.json"
    assert path.is_file() and ref.path == ".perjury/runs/run-evidence/evidence.json"
    assert ref.bytes == path.stat().st_size and ref.complete is True
    assert sorted(p.name for p in path.parent.iterdir()) == ["evidence.json"]  # no temp leftovers
    loaded = store.load("run-evidence")
    assert verify_bundle_hash(loaded) and loaded["bundle_sha256"] == ref.bundle_sha256
    assert store.load("run-missing") is None


def test_interrupted_write_never_corrupts_or_leaves_temp_files(tmp_path, monkeypatch) -> None:
    target = tmp_path / "runs" / "r" / "evidence.json"
    write_json_atomic(target, '{"v": 1}\n')

    def boom(src, dst):
        raise OSError("disk full during rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_json_atomic(target, '{"v": 2}\n')
    monkeypatch.undo()
    assert json.loads(target.read_text()) == {"v": 1}  # old content intact, never partial
    assert [p.name for p in target.parent.iterdir()] == ["evidence.json"]


def test_unsafe_run_ids_cannot_escape_the_evidence_root(tmp_path) -> None:
    store = EvidenceStore(tmp_path / ".perjury", SPEC)
    for bad in ("../evil", "a/b", "..", "", "x" * 100, "run id"):
        with pytest.raises(ValueError):
            store.path_for(bad)
        assert store.load(bad) is None
    assert not (tmp_path / "evil").exists()


def test_evidence_dir_is_gitignored() -> None:
    assert ".perjury/" in (ROOT / ".gitignore").read_text().splitlines()


# ------------------------------------------------------------------ API integration


def api_client(tmp_path: Path, **kw) -> tuple[TestClient, EvidenceStore]:
    store = EvidenceStore(tmp_path / ".perjury", SPEC, "abc1234", env={})
    manager = make_manager(**kw)
    manager.evidence = store
    return TestClient(create_app(manager)), store


def test_finished_run_writes_evidence_before_the_terminal_event(tmp_path) -> None:
    client, store = api_client(tmp_path)
    run_id = start(client)
    read_sse(client, f"/api/runs/{run_id}/events")  # returns at the terminal event
    snap = client.get(f"/api/runs/{run_id}").json()  # fetched immediately after
    assert snap["evidence_path"] == f".perjury/runs/{run_id}/evidence.json"
    assert snap["evidence_error"] is None
    on_disk = store.load(run_id)
    assert snap["evidence_sha256"] == on_disk["bundle_sha256"] and verify_bundle_hash(on_disk)
    assert on_disk["run"]["rescore"]["comparison"]["after"]["score"] == 0.75


def test_evidence_endpoint_exports_the_same_sanitized_bundle(tmp_path) -> None:
    client, store = api_client(tmp_path)
    run_id = start(client)
    wait_terminal(client, run_id)
    response = client.get(f"/api/runs/{run_id}/evidence")
    assert response.status_code == 200 and response.json() == store.load(run_id)
    assert client.get("/api/runs/nope/evidence").json()["detail"]["code"] == "run_not_found"


def test_evidence_endpoint_is_typed_404_without_a_store() -> None:
    client = TestClient(create_app(make_manager()))
    run_id = start(client)
    wait_terminal(client, run_id)
    response = client.get(f"/api/runs/{run_id}/evidence")
    assert (
        response.status_code == 404 and response.json()["detail"]["code"] == "evidence_unavailable"
    )


def test_failed_run_and_crashed_run_still_persist_evidence(tmp_path) -> None:
    client, store = api_client(tmp_path, executor=ScriptedExecutor(baseline=O.TEST_FAIL))
    run_id = start(client)
    wait_terminal(client, run_id)
    assert store.load(run_id)["outcome"]["reason"] == "baseline_not_ready"

    def crash(run_id, on_event):
        on_event  # noqa: B018
        raise RuntimeError("kaboom")

    crashing = RunManager(crash, evidence=EvidenceStore(tmp_path / "c", SPEC, "abc", env={}))
    crash_client = TestClient(create_app(crashing))
    crash_id = start(crash_client)
    wait_terminal(crash_client, crash_id)
    saved = crashing.evidence.load(crash_id)
    assert saved["crash"]["code"] == "internal_error" and "kaboom" in saved["crash"]["message"]


def test_evidence_storage_failure_never_changes_the_run_outcome(tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    manager = make_manager()
    manager.evidence = EvidenceStore(blocker / "sub", SPEC, "abc", env={})  # cannot mkdir
    client = TestClient(create_app(manager))
    snap = wait_terminal(client, start(client))
    assert snap["stage"] == "verified" and snap["evidence_path"] is None
    assert snap["evidence_error"]


def test_importing_the_api_does_not_shell_out_or_touch_the_evidence_dir() -> None:
    code = (
        "import subprocess\n"
        "calls = []\n"
        "orig = subprocess.run\n"
        "subprocess.run = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]\n"
        "import perjury.api\n"
        "assert not calls, calls\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stdout + done.stderr


def test_commit_sha_is_marked_dirty_when_tracked_files_differ(tmp_path) -> None:
    from perjury.api import _commit_sha

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    clean = _commit_sha(tmp_path)
    assert clean and not clean.endswith("-dirty")
    (tmp_path / "f.txt").write_text("two\n")
    assert _commit_sha(tmp_path) == f"{clean}-dirty"
    (tmp_path / "untracked.txt").write_text("x")  # untracked files do not count
    git("checkout", "--", "f.txt")
    assert _commit_sha(tmp_path) == clean
    assert _commit_sha(tmp_path / "missing") is None
