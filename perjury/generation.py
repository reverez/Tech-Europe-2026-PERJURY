"""Injectable test-generation boundary (survivor -> TestProposal) for the orchestrator.

The model only proposes text; ``candidate.py`` decides whether it may be materialized and
``hardening.py`` decides whether it proves anything. Live use resolves the existing lazy
``test_agent`` (single PERJURY_MODEL source); tests inject a fake with no credentials or network.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .analysis import SurvivorSelection
from .context import ContextBudget, pack_context, render_context_request
from .contracts import BaselineResult, TestProposal, WorkspaceSpec
from .planning import MutationPlan
from .workspace import WorkspaceError


class GenerationFailureCode(StrEnum):
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"
    NO_TARGET = "no_target"


class GenerationError(WorkspaceError):
    def __init__(self, code: GenerationFailureCode, message: str) -> None:
        self.code = code
        super().__init__(f"[{code.value}] {message}")


class GenerationRequest(BaseModel):
    mutation_id: str
    prompt: str


class TestGenerator(Protocol):
    """Injectable model boundary. Returns a TestProposal (or a mapping validated as one)."""

    __test__ = False

    def generate(self, request: GenerationRequest) -> TestProposal | dict[str, Any]: ...


class GeminiTestGenerator:
    """Live boundary; the agent is resolved lazily on first use."""

    __test__ = False

    def generate(self, request: GenerationRequest) -> TestProposal:
        from .agent import test_agent

        return test_agent.run_sync(request.prompt).output


def build_generation_request(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: MutationPlan,
    selection: SurvivorSelection,
    budget: ContextBudget | None = None,
) -> GenerationRequest:
    if selection.target is None:
        raise GenerationError(GenerationFailureCode.NO_TARGET, "no hardening target was selected")
    target = selection.target
    proposal = next(m for m in plan.batch.mutations if m.id == target.mutation_id)
    bundle = pack_context(spec, budget=budget, manifest=baseline.manifest)
    tests = ", ".join(bundle.manifest.context_only_paths) or "(none included)"
    prompt = render_context_request(
        bundle,
        intro="You are writing one pytest regression test for a potential test gap.",
        instructions=(
            (
                f"Mutation {target.mutation_id} survived the existing suite. Write ONE focused "
                "pytest test that PASSES on the original code and FAILS on the mutant."
            ),
            f"Set target_file to exactly one of these existing test files: {tests}",
            (
                "Import from repository modules the same way the existing tests do. Be "
                "deterministic; no network, files, sleeps or randomness. Do not modify "
                "implementation files."
            ),
            f"Return mutation_id exactly '{target.mutation_id}'.",
        ),
        extra_sections=(
            (
                f"SURVIVING MUTATION {target.mutation_id}",
                (
                    f"description: {proposal.description}\nhypothesis: {proposal.hypothesis}\n"
                    f"diff:\n{target.diff}\n"
                    f"analysis gap: {target.analysis.behavioural_gap}\n"
                    f"test intent: {target.analysis.test_intent}"
                ),
            ),
        ),
    )
    return GenerationRequest(mutation_id=target.mutation_id, prompt=prompt)


def generate_test(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: MutationPlan,
    selection: SurvivorSelection,
    generator: TestGenerator,
    budget: ContextBudget | None = None,
) -> TestProposal:
    request = build_generation_request(spec, baseline, plan, selection, budget)
    try:
        raw = generator.generate(request)
    except Exception as exc:
        raise GenerationError(
            GenerationFailureCode.PROVIDER_ERROR, f"generator failed: {exc!r}"
        ) from exc
    try:
        proposal = TestProposal.model_validate(
            raw.model_dump() if isinstance(raw, TestProposal) else raw
        )
    except ValidationError as exc:
        raise GenerationError(
            GenerationFailureCode.INVALID_OUTPUT, f"invalid TestProposal: {exc.errors()[0]['msg']}"
        ) from exc
    if proposal.mutation_id != request.mutation_id:
        raise GenerationError(
            GenerationFailureCode.INVALID_OUTPUT,
            f"proposal mutation_id {proposal.mutation_id!r} != {request.mutation_id!r}",
        )
    return proposal
