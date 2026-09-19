"""Deterministic survivor analysis and hardening-target selection (#12).

Only SURVIVED mutants are candidates. They are analysed one at a time in stable mutation-ID order
(numeric, so M9 < M10) through an injectable analyzer boundary. Every analysis is retained as
evidence. Candidates the analyzer marks ``possibly_equivalent`` are skipped, never forced into
verification; the first non-equivalent survivor becomes the hardening target. The number of
analyses is bounded by the survivor set.

A survivor is a *potential test gap*, not a confirmed bug: the analysis filters likely
equivalence but never proves it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError, model_validator

from .context import ContextBudget, pack_context, render_context_request
from .contracts import BaselineResult, MutationStatus, SurvivorAnalysis, WorkspaceSpec
from .execution import MutationExecutionReport, MutationTrial
from .planning import MutationPlan
from .workspace import WorkspaceError

POTENTIAL_TEST_GAP = "potential test gap"


class AnalysisFailureCode(StrEnum):
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"
    INCONSISTENT_INPUTS = "inconsistent_inputs"


class SelectionStatus(StrEnum):
    SELECTED = "selected"
    NO_SURVIVORS = "no_survivors"
    ALL_POSSIBLY_EQUIVALENT = "all_possibly_equivalent"


class SurvivorAnalysisError(WorkspaceError):
    """Typed failure of the analysis stage (provider/contract problems, not 'no target')."""

    def __init__(
        self,
        code: AnalysisFailureCode,
        message: str,
        *,
        analyses: tuple[SurvivorAnalysis, ...] = (),
    ) -> None:
        self.code = code
        self.analyses = analyses
        super().__init__(f"[{code.value}] {message}")


class AnalysisRequest(BaseModel):
    """What an analyzer receives; ``prompt`` already delimits repository text as untrusted."""

    mutation_id: str
    prompt: str


class SurvivorAnalyzer(Protocol):
    """Injectable model boundary. Returns a SurvivorAnalysis (or a mapping validated as one)."""

    def analyze(self, request: AnalysisRequest) -> SurvivorAnalysis | dict[str, Any]: ...


class GeminiSurvivorAnalyzer:
    """Live boundary. The agent (single PERJURY_MODEL source) is resolved lazily on first use."""

    def analyze(self, request: AnalysisRequest) -> SurvivorAnalysis:
        from .agent import survivor_agent

        return survivor_agent.run_sync(request.prompt).output


class HardeningTarget(BaseModel):
    mutation_id: str
    target_path: str
    diff: str
    analysis: SurvivorAnalysis
    label: str = POTENTIAL_TEST_GAP
    summary: str


class SurvivorSelection(BaseModel):
    status: SelectionStatus
    candidate_ids: tuple[str, ...]  # SURVIVED ids in analysis order
    analyses: tuple[SurvivorAnalysis, ...]  # every analysis performed, in order
    skipped_equivalent_ids: tuple[str, ...] = ()
    target: HardeningTarget | None = None
    message: str

    @model_validator(mode="after")
    def outcome_must_be_consistent(self) -> SurvivorSelection:
        analysed = [a.mutation_id for a in self.analyses]
        if analysed != list(self.candidate_ids[: len(analysed)]):
            raise ValueError("Analyses must follow candidate order without gaps.")
        if self.status is SelectionStatus.SELECTED:
            if self.target is None or self.target.mutation_id != analysed[-1]:
                raise ValueError("A selected outcome must target the last analysed survivor.")
            if self.target.analysis.possibly_equivalent:
                raise ValueError("A possibly-equivalent survivor cannot be the target.")
        elif self.target is not None:
            raise ValueError("Only a selected outcome may carry a target.")
        if self.status is SelectionStatus.NO_SURVIVORS and self.candidate_ids:
            raise ValueError("no_survivors cannot have candidates.")
        if self.status is SelectionStatus.ALL_POSSIBLY_EQUIVALENT and (
            not self.candidate_ids
            or len(self.analyses) != len(self.candidate_ids)
            or not all(a.possibly_equivalent for a in self.analyses)
        ):
            raise ValueError(
                "all_possibly_equivalent requires every candidate analysed and skipped."
            )
        return self


def mutation_id_sort_key(mutation_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"M(\d+)", mutation_id)
    return (int(match.group(1)) if match else 10**9, mutation_id)


def _analysis_request(
    spec: WorkspaceSpec,
    bundle_budget: ContextBudget | None,
    baseline: BaselineResult,
    plan: MutationPlan,
    trial: MutationTrial,
) -> AnalysisRequest:
    bundle = pack_context(spec, budget=bundle_budget, manifest=baseline.manifest)
    proposal = next(m for m in plan.batch.mutations if m.id == trial.mutation_id)
    prompt = render_context_request(
        bundle,
        intro="You are analysing one mutation that survived an existing pytest suite.",
        instructions=(
            (
                f"Analyse mutation {trial.mutation_id}. Explain the behavioural distinction it "
                "may expose that the tests never check, and state a precise test intent."
            ),
            (
                f"Describe the finding only as a '{POTENTIAL_TEST_GAP}'. Never call it a bug or "
                "a confirmed defect: a surviving mutant may be semantically equivalent."
            ),
            (
                "Set possibly_equivalent=true if the mutant cannot change behaviour over the "
                "valid input domain (or you cannot tell it apart), otherwise false."
            ),
            f"Return mutation_id exactly '{trial.mutation_id}'.",
        ),
        extra_sections=(
            (
                f"MUTATION {trial.mutation_id} (proposed description / hypothesis / diff)",
                (
                    f"description: {proposal.description}\nhypothesis: {proposal.hypothesis}\n"
                    f"diff:\n{trial.diff}"
                ),
            ),
        ),
    )
    return AnalysisRequest(mutation_id=trial.mutation_id, prompt=prompt)


def select_hardening_target(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    plan: MutationPlan,
    report: MutationExecutionReport,
    analyzer: SurvivorAnalyzer,
    *,
    context_budget: ContextBudget | None = None,
) -> SurvivorSelection:
    """Analyse survivors in stable order and select the first suitable hardening target."""
    if report.context_sha256 != plan.context_sha256 or (
        report.base_manifest_sha256 != baseline.manifest_sha256
    ):
        raise SurvivorAnalysisError(
            AnalysisFailureCode.INCONSISTENT_INPUTS,
            "execution report does not belong to this plan/baseline.",
        )
    planned = {m.id for m in plan.batch.mutations}
    if {t.mutation_id for t in report.trials} != planned:
        raise SurvivorAnalysisError(
            AnalysisFailureCode.INCONSISTENT_INPUTS,
            "execution report IDs do not match the accepted plan.",
        )

    survivors = sorted(
        (t for t in report.trials if t.status is MutationStatus.SURVIVED),
        key=lambda t: mutation_id_sort_key(t.mutation_id),
    )
    candidate_ids = tuple(t.mutation_id for t in survivors)
    if not survivors:
        return SurvivorSelection(
            status=SelectionStatus.NO_SURVIVORS,
            candidate_ids=(),
            analyses=(),
            message="No survived mutants: no actionable test gap was found in this batch.",
        )

    analyses: list[SurvivorAnalysis] = []
    skipped: list[str] = []
    for trial in survivors:
        request = _analysis_request(spec, context_budget, baseline, plan, trial)
        try:
            raw = analyzer.analyze(request)
        except Exception as exc:
            raise SurvivorAnalysisError(
                AnalysisFailureCode.PROVIDER_ERROR,
                f"analyzer failed on {trial.mutation_id}: {exc!r}",
                analyses=tuple(analyses),
            ) from exc
        try:
            analysis = (
                SurvivorAnalysis.model_validate(raw.model_dump())
                if isinstance(raw, SurvivorAnalysis)
                else SurvivorAnalysis.model_validate(raw)
            )
        except ValidationError as exc:
            raise SurvivorAnalysisError(
                AnalysisFailureCode.INVALID_OUTPUT,
                f"analysis for {trial.mutation_id} failed validation: {exc.errors()[0]['msg']}",
                analyses=tuple(analyses),
            ) from exc
        if analysis.mutation_id != trial.mutation_id:
            raise SurvivorAnalysisError(
                AnalysisFailureCode.INVALID_OUTPUT,
                f"analysis mutation_id {analysis.mutation_id!r} != requested {trial.mutation_id!r}",
                analyses=tuple(analyses),
            )
        analyses.append(analysis)
        if analysis.possibly_equivalent:
            skipped.append(trial.mutation_id)
            continue
        target = HardeningTarget(
            mutation_id=trial.mutation_id,
            target_path=trial.target_path,
            diff=trial.diff,
            analysis=analysis,
            summary=f"Potential test gap in {trial.target_path}: {analysis.behavioural_gap}",
        )
        return SurvivorSelection(
            status=SelectionStatus.SELECTED,
            candidate_ids=candidate_ids,
            analyses=tuple(analyses),
            skipped_equivalent_ids=tuple(skipped),
            target=target,
            message=f"Selected {trial.mutation_id} as the hardening target ({POTENTIAL_TEST_GAP}).",
        )

    return SurvivorSelection(
        status=SelectionStatus.ALL_POSSIBLY_EQUIVALENT,
        candidate_ids=candidate_ids,
        analyses=tuple(analyses),
        skipped_equivalent_ids=tuple(skipped),
        message=(
            "Every survived mutant was flagged possibly equivalent; no actionable test gap "
            "was selected (equivalence is not proven)."
        ),
    )
