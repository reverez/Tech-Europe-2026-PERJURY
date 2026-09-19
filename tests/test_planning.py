from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from perjury.contracts import MutationBatch, MutationProposal, WorkspaceSpec
from perjury.planning import (
    MutationPlan,
    PlanningConfig,
    PlanningError,
    PlanningFailureCode,
    PlanningRequest,
    RejectionReason,
    plan_mutations,
)
from perjury.workspace import BaselineResult, LocalPytestExecutor, run_baseline

SOURCE = """def a(x: int) -> int:
    return x + 1


def b(x: int) -> int:
    return x - 1


def c(x: int) -> int:
    return x * 2


def d(x: int) -> int:
    return x // 2


def e(x: int) -> int:
    return x % 3


def f(x: int) -> int:
    return x ** 2


def g(x: int) -> int:
    return x & 1


def h(x: int) -> int:
    return x | 1


def i(x: int) -> int:
    return -x


def dup(x: int) -> int:
    return x + 0
    return x + 0
"""
TESTS = "from src.app import a\n\n\ndef test_a() -> None:\n    assert a(1) == 2\n"

# (original, mutated) pairs that each apply exactly once and compile.
GOOD = [
    ("return x + 1", "return x + 2"),
    ("return x - 1", "return x - 2"),
    ("return x * 2", "return x * 3"),
    ("return x // 2", "return x // 3"),
    ("return x % 3", "return x % 4"),
    ("return x ** 2", "return x ** 3"),
    ("return x & 1", "return x & 3"),
    ("return x | 1", "return x | 2"),
    ("return -x", "return +x"),
    ("def a(x: int) -> int:", "def a(x: int, y: int = 0) -> int:"),
]


def proposal(
    pid: str, original: str, mutated: str, path: str = "src/app.py", **kw: Any
) -> MutationProposal:
    return MutationProposal(
        id=pid,
        file_path=path,
        description=kw.get("description", f"{original} -> {mutated}"),
        hypothesis="h",
        original_snippet=original,
        mutated_snippet=mutated,
    )


def batch(*proposals: MutationProposal) -> MutationBatch:
    return MutationBatch(rationale="r", mutations=list(proposals))


def good(n: int, start: int = 0, id_offset: int = 0) -> list[MutationProposal]:
    return [proposal(f"M{i + 1 + id_offset:02d}", *GOOD[start + i]) for i in range(n)]


class FakePlanner:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[PlanningRequest] = []

    def propose(self, request: PlanningRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def env(tmp_path: Path) -> tuple[WorkspaceSpec, BaselineResult]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(SOURCE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(TESTS)
    (tmp_path / "src" / "__init__.py").write_text("")
    spec = WorkspaceSpec(
        workspace_id="plan-unit",
        source_root=str(tmp_path),
        snapshot_id="fixture:plan-v1",
        pytest_argv=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"),
        mutable_paths=("src",),
        context_paths=("tests",),
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    return spec, run_baseline(spec, LocalPytestExecutor())


def reasons(error_or_plan: PlanningError | MutationPlan) -> list[RejectionReason]:
    rejected = error_or_plan.rejected
    return [r.reason for r in rejected]


def test_valid_batch_is_accepted_with_stable_sequential_ids(env) -> None:
    spec, baseline = env
    messy = [proposal(f"M{i + 40}", *GOOD[i]) for i in range(8)]
    planner = FakePlanner(batch(*messy))
    plan = plan_mutations(spec, baseline, planner)
    assert [m.id for m in plan.batch.mutations] == [f"M0{i}" for i in range(1, 9)]
    assert plan.proposed_ids["M01"] == "M40"
    assert [a.mutation_id for a in plan.applied] == [m.id for m in plan.batch.mutations]
    assert plan.replenishments_used == 0 and plan.rejected == ()
    assert len(planner.requests) == 1 and planner.requests[0].requested_count == 8
    again = plan_mutations(spec, baseline, FakePlanner(batch(*messy)))
    assert again.model_dump() == plan.model_dump()  # stable and deterministic


def test_prompt_uses_context_packer_with_untrusted_delimiters(env) -> None:
    spec, baseline = env
    planner = FakePlanner(batch(*good(8)))
    plan = plan_mutations(spec, baseline, planner)
    prompt = planner.requests[0].prompt
    assert "BEGIN UNTRUSTED REPOSITORY CONTENT" in prompt
    assert plan.context_sha256 in prompt
    assert "role=CONTEXT-ONLY" in prompt


def test_target_count_is_bounded_and_defaults_to_eight() -> None:
    assert PlanningConfig().target_count == 8
    for bad in (5, 11):
        with pytest.raises(ValueError):
            PlanningConfig(target_count=bad)
    assert PlanningConfig(target_count=6).target_count == 6


def test_target_count_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERJURY_MUTATION_COUNT", raising=False)
    assert PlanningConfig.from_env().target_count == 8
    monkeypatch.setenv("PERJURY_MUTATION_COUNT", "10")
    assert PlanningConfig.from_env().target_count == 10
    monkeypatch.setenv("PERJURY_MUTATION_COUNT", "3")
    with pytest.raises(ValueError):
        PlanningConfig.from_env()


def test_excess_valid_proposals_are_capped_at_target(env) -> None:
    spec, baseline = env
    plan = plan_mutations(
        spec, baseline, FakePlanner(batch(*good(10))), PlanningConfig(target_count=7)
    )
    assert len(plan.batch.mutations) == 7
    assert reasons(plan) == [RejectionReason.OVER_TARGET] * 3


def test_forbidden_test_target_and_unsafe_paths_are_rejected(env) -> None:
    spec, baseline = env
    bad = [
        proposal("M50", "assert a(1) == 2", "assert a(1) == 3", path="tests/test_app.py"),
        proposal("M51", "x", "y", path="../outside.py"),
        proposal("M52", "x", "y", path="/etc/passwd"),
        proposal("M53", "x", "y", path="setup.py"),
        proposal("M54", "x", "y", path="src\\app.py"),
    ]
    plan = plan_mutations(spec, baseline, FakePlanner(batch(*bad, *good(6))))
    assert reasons(plan) == [
        RejectionReason.FORBIDDEN_TARGET,
        RejectionReason.UNSAFE_PATH,
        RejectionReason.UNSAFE_PATH,
        RejectionReason.FORBIDDEN_TARGET,
        RejectionReason.UNSAFE_PATH,
    ]
    assert len(plan.batch.mutations) == 6


def test_no_op_missing_and_ambiguous_anchors_are_rejected(env) -> None:
    spec, baseline = env
    bad = [
        proposal("M50", "return x + 1", "return x + 1"),
        proposal("M51", "return x + 99", "return x + 100"),
        proposal("M52", "return x + 0", "return x + 5"),  # appears twice
        proposal("M53", "", "return x"),
    ]
    plan = plan_mutations(spec, baseline, FakePlanner(batch(*bad, *good(6))))
    assert reasons(plan) == [
        RejectionReason.NO_OP,
        RejectionReason.MISSING_ANCHOR,
        RejectionReason.AMBIGUOUS_ANCHOR,
        RejectionReason.MISSING_ANCHOR,
    ]


def test_compile_invalid_mutation_does_not_count(env) -> None:
    spec, baseline = env
    bad = proposal("M50", "return x + 1", "return x +")
    plan = plan_mutations(spec, baseline, FakePlanner(batch(bad, *good(9, start=1)[:6])))
    assert reasons(plan) == [RejectionReason.COMPILE_INVALID]
    assert len(plan.batch.mutations) == 6


def test_duplicates_and_duplicate_ids_are_rejected(env) -> None:
    spec, baseline = env
    first = proposal("M01", *GOOD[0])
    same_content = proposal("M02", *GOOD[0])
    same_id = proposal("M01", *GOOD[1])
    plan = plan_mutations(
        spec, baseline, FakePlanner(batch(first, same_content, same_id, *good(7, start=2)))
    )
    assert reasons(plan)[:2] == [RejectionReason.DUPLICATE, RejectionReason.DUPLICATE_ID]
    assert len({m.id for m in plan.batch.mutations}) == len(plan.batch.mutations)


def test_identical_resulting_file_is_a_duplicate(env) -> None:
    spec, baseline = env
    a = proposal("M01", "return x + 1", "return x + 2")
    b = proposal(
        "M02", "def a(x: int) -> int:\n    return x + 1", "def a(x: int) -> int:\n    return x + 2"
    )
    plan = plan_mutations(spec, baseline, FakePlanner(batch(a, b, *good(6, start=1, id_offset=10))))
    assert RejectionReason.DUPLICATE in reasons(plan)
    assert len(plan.batch.mutations) == 7


def test_partial_rejection_triggers_exactly_one_refill_for_the_missing_count(env) -> None:
    spec, baseline = env
    first = batch(*good(4), proposal("M05", "x", "y", path="tests/test_app.py"))
    # refill reuses IDs M01.. (must not collide) and repeats one earlier mutation (must be dropped)
    refill = batch(proposal("M20", *GOOD[0]), *good(3, start=4))
    planner = FakePlanner(first, refill)
    plan = plan_mutations(spec, baseline, planner)
    assert plan.replenishments_used == 1
    assert len(planner.requests) == 2
    assert planner.requests[1].attempt == 2 and planner.requests[1].requested_count == 4
    assert len(plan.batch.mutations) == 7
    assert [m.id for m in plan.batch.mutations] == [f"M0{i}" for i in range(1, 8)]
    dup = [r for r in plan.rejected if r.reason is RejectionReason.DUPLICATE]
    assert len(dup) == 1 and dup[0].attempt == 2


def test_refill_request_lists_prior_accepted_and_rejected_as_avoid(env) -> None:
    spec, baseline = env
    bad = proposal("M05", "return x + 99", "return x + 100")
    planner = FakePlanner(batch(*good(4), bad), batch(*good(4, start=4)))
    plan_mutations(spec, baseline, planner)
    avoid = planner.requests[1].avoid
    assert len(avoid) == 5
    assert {
        "file_path": "src/app.py",
        "original": "return x + 99",
        "mutated": "return x + 100",
    } in [dict(a) for a in avoid]
    assert planner.requests[0].avoid == ()


def test_refill_failure_is_typed_and_makes_no_third_request(env) -> None:
    spec, baseline = env
    planner = FakePlanner(batch(*good(3)), batch(*good(2, start=3)), batch(*good(5, start=5)))
    with pytest.raises(PlanningError) as caught:
        plan_mutations(spec, baseline, planner)
    assert caught.value.code is PlanningFailureCode.INSUFFICIENT_VALID_MUTATIONS
    assert caught.value.accepted == 5
    assert len(planner.requests) == 2
    assert len(planner.responses) == 1


def test_refill_cannot_resurrect_a_rejected_mutation(env) -> None:
    spec, baseline = env
    bad = proposal("M01", "return x + 1", "return x +")  # compile invalid
    planner = FakePlanner(
        batch(bad, *good(4, start=1, id_offset=1)), batch(bad, *good(1, start=5, id_offset=1))
    )
    with pytest.raises(PlanningError) as caught:
        plan_mutations(spec, baseline, planner)
    assert caught.value.accepted == 5
    assert reasons(caught.value).count(RejectionReason.COMPILE_INVALID) == 1
    assert RejectionReason.DUPLICATE in reasons(caught.value)


def test_invalid_structured_output_is_recorded_and_can_be_refilled(env) -> None:
    spec, baseline = env
    planner = FakePlanner({"rationale": "r", "mutations": [{"id": "bad"}]}, batch(*good(8)))
    plan = plan_mutations(spec, baseline, planner)
    assert plan.replenishments_used == 1
    assert plan.rejected[0].reason is RejectionReason.INVALID_OUTPUT


def test_dict_output_is_validated_like_a_live_result(env) -> None:
    spec, baseline = env
    raw = batch(*good(8)).model_dump()
    assert len(plan_mutations(spec, baseline, FakePlanner(raw)).batch.mutations) == 8


def test_provider_exception_is_a_typed_failure_without_retry(env) -> None:
    spec, baseline = env
    planner = FakePlanner(RuntimeError("quota"), batch(*good(8)))
    with pytest.raises(PlanningError) as caught:
        plan_mutations(spec, baseline, planner)
    assert caught.value.code is PlanningFailureCode.PROVIDER_ERROR
    assert len(planner.requests) == 1


def test_non_python_target_is_unsupported(env, tmp_path: Path) -> None:
    spec, baseline = env
    (tmp_path / "src" / "data.txt").write_text("hello\n")
    # snapshot changed after baseline -> re-baseline so the fixture stays valid
    baseline = run_baseline(spec, LocalPytestExecutor())
    bad = proposal("M50", "hello", "bye", path="src/data.txt")
    plan = plan_mutations(spec, baseline, FakePlanner(batch(bad, *good(6))))
    assert reasons(plan) == [RejectionReason.UNSUPPORTED_TARGET]


def test_stale_baseline_fails_typed(env) -> None:
    spec, baseline = env
    (Path(spec.source_root) / "src" / "app.py").write_text(SOURCE + "\n# drift\n")
    with pytest.raises(Exception, match="manifest|snapshot"):
        plan_mutations(spec, baseline, FakePlanner(batch(*good(8))))


def test_plan_serializes_for_evidence(env) -> None:
    spec, baseline = env
    plan = plan_mutations(spec, baseline, FakePlanner(batch(*good(8))))
    assert MutationPlan.model_validate_json(plan.model_dump_json()) == plan


def test_importing_planning_does_not_construct_the_live_agent() -> None:
    import subprocess
    import sys

    code = "import sys, perjury.planning; sys.exit('perjury.agent' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
