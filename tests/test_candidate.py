from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from perjury.candidate import (
    CandidateReason,
    CandidateRejected,
    plan_candidate,
    preflight_candidate,
    write_candidate,
)
from perjury.contracts import ExecutionOutcome, ExecutionResult, WorkspaceSpec
from perjury.contracts import TestProposal as GeneratedTest
from perjury.mutation import materialize_baseline_workspace
from perjury.workspace import (
    BaselineResult,
    LocalPytestExecutor,
    build_snapshot_manifest,
    refund_workspace_spec,
    run_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
GOOD = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_premium_outside_window() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
)


def tp(code: str = GOOD, target: str = "examples/refund/test_refund.py", mid: str = "M01"):
    return GeneratedTest(
        mutation_id=mid,
        test_name="test_premium_outside_window",
        target_file=target,
        test_code=code,
        explanation="e",
    )


@pytest.fixture(scope="module")
def refund() -> tuple[WorkspaceSpec, BaselineResult]:
    spec = refund_workspace_spec(str(ROOT))
    return spec, run_baseline(spec, LocalPytestExecutor())


@pytest.fixture
def tmp_world(tmp_path: Path) -> tuple[WorkspaceSpec, BaselineResult]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def value() -> int:\n    return 1\n")
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from src.app import value\n\n\ndef test_v() -> None:\n    assert value() == 1\n"
    )
    spec = WorkspaceSpec(
        workspace_id="cand-unit",
        source_root=str(tmp_path),
        snapshot_id="fixture:cand-v1",
        pytest_argv=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"),
        mutable_paths=("src",),
        context_paths=("tests",),
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    return spec, run_baseline(spec, LocalPytestExecutor())


def reject(spec, baseline, proposal) -> CandidateRejected:
    with pytest.raises(CandidateRejected) as caught:
        plan_candidate(spec, baseline, proposal)
    return caught.value


def test_safe_create_derives_deterministic_sibling_and_leaves_repo_untouched(refund) -> None:
    spec, baseline = refund
    before = build_snapshot_manifest(spec).manifest_sha256
    plan = plan_candidate(spec, baseline, tp())
    assert plan.candidate_path == "examples/refund/test_perjury_M01.py"
    assert plan == plan_candidate(spec, baseline, tp())
    assert plan.sha256 == hashlib.sha256(plan.code.encode()).hexdigest()
    assert "+++ b/examples/refund/test_perjury_M01.py" in plan.diff
    assert "--- /dev/null" in plan.diff and "+def test_premium_outside_window" in plan.diff

    root = materialize_baseline_workspace(spec, baseline)
    try:
        written = write_candidate(root, plan)
        assert written.read_text() == plan.code
        assert (root / "examples/refund/test_refund.py").read_bytes() == (
            ROOT / "examples/refund/test_refund.py"
        ).read_bytes()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    assert not (ROOT / "examples/refund/test_perjury_M01.py").exists()
    assert build_snapshot_manifest(spec).manifest_sha256 == before


def test_missing_trailing_newline_is_normalised_deterministically(refund) -> None:
    spec, baseline = refund
    assert plan_candidate(spec, baseline, tp(GOOD.rstrip("\n"))).code == GOOD


def test_collision_gets_a_deterministic_suffix_and_never_touches_existing(tmp_world) -> None:
    spec, baseline = tmp_world
    root = Path(spec.source_root)
    existing = root / "tests" / "test_perjury_M01.py"
    existing.write_text("# user file\n")
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_candidate(spec, baseline, tp("def test_x():\n    pass\n", "tests/test_app.py"))
    assert plan.candidate_path == "tests/test_perjury_M01_2.py"
    assert existing.read_text() == "# user file\n"

    isolated = materialize_baseline_workspace(spec, baseline)
    try:
        # simulate a same-name file appearing in the isolated workspace: create-new must refuse
        (isolated / plan.candidate_path).write_text("# precious\n")
        with pytest.raises(CandidateRejected) as caught:
            write_candidate(isolated, plan)
        assert caught.value.reason is CandidateReason.ALREADY_EXISTS
        assert (isolated / plan.candidate_path).read_text() == "# precious\n"
    finally:
        import shutil

        shutil.rmtree(isolated, ignore_errors=True)


@pytest.mark.parametrize(
    "target,reason",
    [
        ("../outside/test_x.py", CandidateReason.UNSAFE_PATH),
        ("/etc/test_x.py", CandidateReason.UNSAFE_PATH),
        ("tests\\test_app.py", CandidateReason.UNSAFE_PATH),
        ("tests/te\x00st.py", CandidateReason.UNSAFE_PATH),
        ("", CandidateReason.UNSAFE_PATH),
        ("tests/../src/app.py", CandidateReason.UNSAFE_PATH),
        ("src/app.py", CandidateReason.FORBIDDEN_TARGET),  # implementation, not a test
        ("README.md", CandidateReason.FORBIDDEN_TARGET),  # outside test/context paths
        (".git/hooks/test_x.py", CandidateReason.FORBIDDEN_TARGET),  # excluded dir
        ("tests/.venv/test_x.py", CandidateReason.FORBIDDEN_TARGET),
        ("tests/__pycache__/test_x.py", CandidateReason.FORBIDDEN_TARGET),
        ("tests/missing.py", CandidateReason.TARGET_MISSING),
    ],
)
def test_unsafe_or_forbidden_targets_are_rejected(tmp_world, target, reason) -> None:
    spec, baseline = tmp_world
    error = reject(spec, baseline, tp("def test_x():\n    pass\n", target))
    assert error.reason is reason
    assert error.outcome is ExecutionOutcome.INVALID


def test_non_python_target_is_rejected(tmp_world) -> None:
    spec, baseline = tmp_world
    (Path(spec.source_root) / "tests" / "data.txt").write_text("x\n")
    baseline = run_baseline(spec, LocalPytestExecutor())
    assert reject(spec, baseline, tp("x = 1\n", "tests/data.txt")).reason is (
        CandidateReason.FORBIDDEN_TARGET
    )


@pytest.mark.parametrize(
    "code,reason",
    [
        ("def test_x(:\n    pass\n", CandidateReason.INVALID_SYNTAX),
        ("   \n", CandidateReason.EMPTY_CODE),
        ("def test_x():\n    pass\x00\n", CandidateReason.INVALID_SYNTAX),
        ("# " + "x" * 70_000 + "\n", CandidateReason.CODE_TOO_LARGE),
    ],
)
def test_invalid_candidate_code_is_rejected_as_invalid(tmp_world, code, reason) -> None:
    spec, baseline = tmp_world
    error = reject(spec, baseline, tp(code, "tests/test_app.py"))
    assert error.reason is reason and error.outcome is ExecutionOutcome.INVALID


def test_symlinked_directory_in_isolated_workspace_cannot_be_escaped(refund, tmp_path) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())
    isolated = materialize_baseline_workspace(spec, baseline)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        directory = isolated / "examples" / "refund"
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
        with pytest.raises(CandidateRejected) as caught:
            write_candidate(isolated, plan)
        assert caught.value.reason is CandidateReason.UNSAFE_PATH
        assert list(outside.iterdir()) == []
    finally:
        import shutil

        shutil.rmtree(isolated, ignore_errors=True)


def test_dangling_symlink_at_candidate_path_is_not_followed(refund, tmp_path) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())
    isolated = materialize_baseline_workspace(spec, baseline)
    victim = tmp_path / "victim.py"
    try:
        os.symlink(victim, isolated / plan.candidate_path)
        with pytest.raises(CandidateRejected) as caught:
            write_candidate(isolated, plan)
        assert caught.value.reason is CandidateReason.ALREADY_EXISTS
        assert not victim.exists()
    finally:
        import shutil

        shutil.rmtree(isolated, ignore_errors=True)


def test_missing_directory_is_never_created(refund) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())
    import tempfile

    empty = Path(tempfile.mkdtemp())
    try:
        with pytest.raises(CandidateRejected) as caught:
            write_candidate(empty, plan)
        assert caught.value.reason is CandidateReason.UNSAFE_PATH
        assert list(empty.iterdir()) == []
    finally:
        import shutil

        shutil.rmtree(empty, ignore_errors=True)


def test_preflight_collects_the_candidate_explicitly(refund) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())
    seen: list[WorkspaceSpec] = []
    real = LocalPytestExecutor()

    class Spy:
        def execute(self, s, *, execution_id, manifest=None):
            seen.append(s)
            return real.execute(s, execution_id=execution_id, manifest=manifest)

    evidence = preflight_candidate(spec, baseline, plan, Spy())
    assert evidence.syntax_ok and evidence.collection.outcome is ExecutionOutcome.PASS
    assert evidence.plan == plan
    assert "--collect-only" in seen[0].pytest_argv
    assert seen[0].pytest_argv[-1] == plan.candidate_path
    assert plan.candidate_path in seen[0].context_paths
    assert not Path(seen[0].source_root).exists()  # throwaway workspace discarded
    assert not (ROOT / plan.candidate_path).exists()


def test_collection_failure_maps_to_invalid_candidate(refund) -> None:
    spec, baseline = refund
    for code in ("import does_not_exist_zzz\n\n\ndef test_x():\n    pass\n", "x = 1\n"):
        plan = plan_candidate(spec, baseline, tp(code))
        with pytest.raises(CandidateRejected) as caught:
            preflight_candidate(spec, baseline, plan, LocalPytestExecutor())
        assert caught.value.reason is CandidateReason.COLLECTION_FAILED
        assert caught.value.outcome is ExecutionOutcome.INVALID
        assert caught.value.execution is not None


@pytest.mark.parametrize("outcome", [ExecutionOutcome.INFRA_ERROR, ExecutionOutcome.TIMEOUT])
def test_preflight_infrastructure_failure_is_not_blamed_on_the_candidate(refund, outcome) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())

    class Broken:
        def execute(self, s, *, execution_id, manifest=None):
            return ExecutionResult(
                execution_id=execution_id, outcome=outcome, duration_ms=1, failure_detail="x"
            )

    with pytest.raises(CandidateRejected) as caught:
        preflight_candidate(spec, baseline, plan, Broken())
    assert caught.value.reason is CandidateReason.PREFLIGHT_UNAVAILABLE
    assert caught.value.outcome is outcome


def test_preflight_rejects_a_plan_from_another_baseline(refund) -> None:
    spec, baseline = refund
    plan = plan_candidate(spec, baseline, tp())
    forged = plan.model_copy(update={"base_manifest_sha256": "sha256:" + "0" * 64})
    with pytest.raises(CandidateRejected):
        preflight_candidate(spec, baseline, forged, LocalPytestExecutor())
