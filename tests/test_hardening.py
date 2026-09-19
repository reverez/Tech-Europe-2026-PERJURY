from __future__ import annotations

import hashlib
import itertools
import threading
from pathlib import Path

import pytest

from perjury import hardening
from perjury.candidate import CandidatePlan, CandidateRejected, plan_candidate, write_candidate
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import ExecutionResult, MutationProposal, VerificationEvidence, WorkspaceSpec
from perjury.contracts import TestProposal as GeneratedTest
from perjury.fanout import FanoutResult
from perjury.hardening import (
    HardeningVerification,
    WorldConsistencyError,
    build_verification_worlds,
    verify_candidate,
)
from perjury.planning import plan_mutations
from perjury.workspace import (
    BaselineResult,
    LocalPytestExecutor,
    build_snapshot_manifest,
    refund_workspace_spec,
    run_baseline,
)

ROOT = Path(__file__).resolve().parents[1]
PREMIUM_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_premium_outside_window() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
)
WEAK_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_inside_window() -> None:\n"
    "    assert calculate_refund(50.0, 5, premium=False) == 50.0\n"
)
WRONG_TEST = (
    "from examples.refund.refund import calculate_refund\n\n\n"
    "def test_wrong() -> None:\n"
    "    assert calculate_refund(100.0, 45, premium=True) == 0.0\n"
)


def generated(code: str, mid: str = "M01") -> GeneratedTest:
    return GeneratedTest(
        mutation_id=mid,
        test_name="t",
        target_file="examples/refund/test_refund.py",
        test_code=code,
        explanation="e",
    )


@pytest.fixture(scope="module")
def refund() -> tuple[WorkspaceSpec, BaselineResult, MutationProposal]:
    spec = refund_workspace_spec(str(ROOT))
    baseline = run_baseline(spec, LocalPytestExecutor())
    plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
    return spec, baseline, plan.batch.mutations[0]  # M01: premium survivor


def candidate(refund, code: str = PREMIUM_TEST, mid: str = "M01") -> CandidatePlan:
    spec, baseline, _ = refund
    return plan_candidate(spec, baseline, generated(code, mid))


def exec_result(outcome: O, eid: str) -> ExecutionResult:
    code = {O.PASS: 0, O.TEST_FAIL: 1, O.INVALID: 2}.get(outcome)
    return ExecutionResult(
        execution_id=eid,
        outcome=outcome,
        exit_code=code,
        duration_ms=3,
        stdout=f"out {outcome.value}",
        failure_detail=None if code is not None else "x",
    )


class MatrixExecutor:
    """Returns scripted outcomes per world and records what each world contained."""

    def __init__(self, original: O, mutant: O) -> None:
        self.by_world = {"original": original, "mutant": mutant}
        self.seen: dict[str, dict] = {}
        self.lock = threading.Lock()

    def execute(self, spec, *, execution_id, manifest=None):
        world = execution_id.split(":")[1]
        root = Path(spec.source_root)
        with self.lock:
            self.seen[world] = {
                "spec": spec,
                "manifest": manifest,
                "candidate": {
                    p: (root / p).read_bytes()
                    for p in spec.pytest_argv
                    if p.endswith(".py") and (root / p).exists() and "perjury" in p
                },
                "refund": (root / "examples/refund/refund.py").read_bytes(),
            }
        return exec_result(self.by_world[world], execution_id)


@pytest.mark.parametrize("original,mutant", list(itertools.product(list(O), list(O))))
def test_full_semantic_verdict_matrix(refund, original, mutant) -> None:
    spec, baseline, mutation = refund
    result = verify_candidate(
        spec, baseline, mutation, candidate(refund), MatrixExecutor(original, mutant)
    )
    verified = original is O.PASS and mutant is O.TEST_FAIL
    assert (result.result.verdict == "verified") is verified
    assert result.result.evidence.verified is verified
    if not verified:
        assert result.result.verdict in {"rejected", "inconclusive"}
    if original is O.TEST_FAIL:
        assert result.result.verdict == "rejected"
    if O.PASS is not original or mutant in {O.INVALID, O.TIMEOUT, O.INFRA_ERROR}:
        assert result.result.verdict != "verified"


def test_worlds_share_baseline_candidate_and_command_and_differ_only_by_the_mutation(
    refund,
) -> None:
    spec, baseline, mutation = refund
    plan = candidate(refund)
    executor = MatrixExecutor(O.PASS, O.TEST_FAIL)
    result = verify_candidate(spec, baseline, mutation, plan, executor)
    original, mutant = executor.seen["original"], executor.seen["mutant"]

    assert original["spec"].pytest_argv == mutant["spec"].pytest_argv
    assert original["spec"].pytest_argv[-1] == plan.candidate_path
    assert original["candidate"] == mutant["candidate"] == {plan.candidate_path: plan.code.encode()}
    assert hashlib.sha256(plan.code.encode()).hexdigest() == plan.sha256
    baseline_source = (ROOT / "examples/refund/refund.py").read_bytes()
    assert original["refund"] == baseline_source
    assert mutant["refund"] != baseline_source
    assert b"or premium" not in mutant["refund"]

    def strip(m):
        return {f.path: f.sha256 for f in m.files}

    difference = {
        p
        for p in strip(original["manifest"])
        if strip(original["manifest"])[p] != strip(mutant["manifest"]).get(p)
    }
    assert difference == {"examples/refund/refund.py"}  # the only source difference
    assert set(strip(original["manifest"])) == set(strip(mutant["manifest"]))

    e = result.result.evidence
    assert e.base_manifest_sha256 == baseline.manifest_sha256
    assert e.candidate_sha256 == plan.sha256 and e.candidate_path == plan.candidate_path
    assert e.original_manifest_sha256 == original["manifest"].manifest_sha256
    assert e.mutant_manifest_sha256 == mutant["manifest"].manifest_sha256
    assert e.original_manifest_sha256 != e.mutant_manifest_sha256
    assert e.pytest_argv == original["spec"].pytest_argv
    assert e.original_stdout == "out pass" and e.mutant_stdout == "out test_fail"


def test_real_pytest_verifies_the_premium_candidate_for_m01(refund) -> None:
    spec, baseline, mutation = refund
    before = build_snapshot_manifest(spec).manifest_sha256
    result = verify_candidate(spec, baseline, mutation, candidate(refund), LocalPytestExecutor())
    assert result.result.verdict == "verified"
    assert result.original.outcome is O.PASS and result.mutant.outcome is O.TEST_FAIL
    assert result.mutant.exit_code == 1
    assert build_snapshot_manifest(spec).manifest_sha256 == before  # repo untouched
    assert not (ROOT / result.candidate_path).exists()
    assert "+++ b/examples/refund/test_perjury_M01.py" in result.candidate_diff
    assert HardeningVerification.model_validate_json(result.model_dump_json()) == result


def test_real_pytest_weak_and_wrong_candidates_do_not_verify(refund) -> None:
    spec, baseline, mutation = refund
    weak = verify_candidate(
        spec, baseline, mutation, candidate(refund, WEAK_TEST), LocalPytestExecutor()
    )
    assert weak.result.verdict == "inconclusive"  # passes both worlds: proves nothing
    assert weak.original.outcome is O.PASS and weak.mutant.outcome is O.PASS
    wrong = verify_candidate(
        spec, baseline, mutation, candidate(refund, WRONG_TEST), LocalPytestExecutor()
    )
    assert wrong.result.verdict == "rejected"
    assert wrong.original.outcome is O.TEST_FAIL


def test_worlds_are_isolated_and_cleaned_up(refund) -> None:
    spec, baseline, mutation = refund
    with build_verification_worlds(spec, baseline, mutation, candidate(refund)) as worlds:
        roots = [worlds.original.root, worlds.mutant.root]
        assert roots[0] != roots[1] and all(r.exists() for r in roots)
        assert str(ROOT) not in {str(r) for r in roots}
    assert not any(r.exists() for r in roots)

    seen: list[Path] = []
    real = hardening.write_candidate

    def exploding(root, plan):
        seen.append(root)
        if len(seen) == 2:
            raise RuntimeError("boom")
        return real(root, plan)

    from unittest import mock

    with (
        mock.patch.object(hardening, "write_candidate", exploding),
        pytest.raises(RuntimeError),
    ):
        build_verification_worlds(spec, baseline, mutation, candidate(refund))
    assert not any(r.exists() for r in seen)


def test_candidate_for_a_different_mutation_is_rejected(refund) -> None:
    spec, baseline, mutation = refund
    with pytest.raises(WorldConsistencyError, match="M02"):
        verify_candidate(
            spec,
            baseline,
            mutation,
            candidate(refund, mid="M02"),
            MatrixExecutor(O.PASS, O.TEST_FAIL),
        )


def test_candidate_planned_against_another_baseline_is_rejected(refund) -> None:
    spec, baseline, mutation = refund
    forged = candidate(refund).model_copy(update={"base_manifest_sha256": "sha256:" + "1" * 64})
    with pytest.raises(WorldConsistencyError, match="different baseline"):
        verify_candidate(spec, baseline, mutation, forged, MatrixExecutor(O.PASS, O.TEST_FAIL))
    other_snapshot = candidate(refund).model_copy(update={"base_snapshot_id": "fixture:other"})
    with pytest.raises(WorldConsistencyError):
        verify_candidate(
            spec, baseline, mutation, other_snapshot, MatrixExecutor(O.PASS, O.TEST_FAIL)
        )


def test_tampered_candidate_code_is_rejected(refund) -> None:
    spec, baseline, mutation = refund
    forged = candidate(refund).model_copy(update={"code": WEAK_TEST})  # bypasses validators
    with pytest.raises(WorldConsistencyError, match="hash"):
        verify_candidate(spec, baseline, mutation, forged, MatrixExecutor(O.PASS, O.TEST_FAIL))


def test_candidate_bytes_that_differ_between_worlds_are_rejected(refund) -> None:
    spec, baseline, mutation = refund
    calls = {"n": 0}
    real = write_candidate

    def sneaky(root, plan):
        calls["n"] += 1
        path = real(root, plan)
        if calls["n"] == 2:  # the mutant world's copy is altered after writing
            path.write_text(plan.code + "\n# extra\n")
        return path

    from unittest import mock

    with (
        mock.patch.object(hardening, "write_candidate", sneaky),
        pytest.raises(WorldConsistencyError, match="candidate bytes differ|differs from"),
    ):
        verify_candidate(
            spec, baseline, mutation, candidate(refund), MatrixExecutor(O.PASS, O.TEST_FAIL)
        )


def test_candidate_path_already_in_baseline_is_rejected(refund) -> None:
    spec, baseline, mutation = refund
    forged = candidate(refund).model_copy(
        update={"candidate_path": "examples/refund/test_refund.py"}
    )
    # create-new refuses to overwrite the existing baseline file before any world can be judged
    with pytest.raises((WorldConsistencyError, CandidateRejected)):
        verify_candidate(spec, baseline, mutation, forged, MatrixExecutor(O.PASS, O.TEST_FAIL))
    assert (ROOT / "examples/refund/test_refund.py").read_text().startswith("from examples")


def test_fanout_must_return_exactly_one_result_per_world(refund) -> None:
    spec, baseline, mutation = refund
    from perjury.fanout import run_fanout

    def short(jobs, executor, **kw):
        good = run_fanout(jobs, executor, **kw)
        return FanoutResult(**{**good.model_dump(), "results": good.results[:1]})

    with pytest.raises(WorldConsistencyError, match="exactly one result"):
        verify_candidate(
            spec,
            baseline,
            mutation,
            candidate(refund),
            MatrixExecutor(O.PASS, O.TEST_FAIL),
            fanout_runner=short,
        )


def test_verification_evidence_identity_fields_are_optional_and_validated() -> None:
    minimal = VerificationEvidence(
        mutation_id="M01",
        original_outcome=O.PASS,
        mutant_outcome=O.TEST_FAIL,
        original_exit_code=0,
        mutant_exit_code=1,
        original_duration_ms=1,
        mutant_duration_ms=1,
    )
    assert minimal.verified and minimal.candidate_sha256 is None
    with pytest.raises(ValueError):
        VerificationEvidence(**{**minimal.model_dump(), "candidate_sha256": "nothex"})
