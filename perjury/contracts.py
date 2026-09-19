from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ExecutionOutcome(StrEnum):
    PASS = "pass"
    TEST_FAIL = "test_fail"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


class MutationStatus(StrEnum):
    KILLED = "killed"
    SURVIVED = "survived"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


def mutation_status_for(outcome: ExecutionOutcome) -> MutationStatus:
    return {
        ExecutionOutcome.PASS: MutationStatus.SURVIVED,
        ExecutionOutcome.TEST_FAIL: MutationStatus.KILLED,
        ExecutionOutcome.INVALID: MutationStatus.INVALID,
        ExecutionOutcome.TIMEOUT: MutationStatus.TIMEOUT,
        ExecutionOutcome.INFRA_ERROR: MutationStatus.INFRA_ERROR,
    }[outcome]


class MutationProposal(BaseModel):
    id: str = Field(pattern=r"^M\d{2,}$")
    file_path: str
    description: str
    hypothesis: str
    original_snippet: str
    mutated_snippet: str


class MutationBatch(BaseModel):
    rationale: str
    mutations: list[MutationProposal] = Field(min_length=1, max_length=20)


class ExecutionResult(BaseModel):
    mutation_id: str
    outcome: ExecutionOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(ge=0)

    @property
    def status(self) -> MutationStatus:
        """Mutation-testing projection retained for UI/demo compatibility."""
        return mutation_status_for(self.outcome)

    @model_validator(mode="after")
    def outcome_must_match_known_pytest_exit(self) -> ExecutionResult:
        if self.outcome is ExecutionOutcome.PASS and self.exit_code != 0:
            raise ValueError("PASS requires pytest exit code 0.")
        if self.outcome is ExecutionOutcome.TEST_FAIL and self.exit_code != 1:
            raise ValueError("TEST_FAIL requires pytest exit code 1.")
        return self


class SurvivorAnalysis(BaseModel):
    mutation_id: str
    behavioural_gap: str
    test_intent: str
    possibly_equivalent: bool = False
    reasoning: str


class TestProposal(BaseModel):
    mutation_id: str
    test_name: str
    target_file: str
    test_code: str
    explanation: str


class VerificationEvidence(BaseModel):
    mutation_id: str
    original_outcome: ExecutionOutcome
    mutant_outcome: ExecutionOutcome
    original_exit_code: int | None = None
    mutant_exit_code: int | None = None
    original_stdout: str = ""
    mutant_stdout: str = ""
    original_duration_ms: int = Field(ge=0)
    mutant_duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def outcomes_must_match_known_pytest_exits(self) -> VerificationEvidence:
        pairs = (
            ("original", self.original_outcome, self.original_exit_code),
            ("mutant", self.mutant_outcome, self.mutant_exit_code),
        )
        for label, outcome, exit_code in pairs:
            if outcome is ExecutionOutcome.PASS and exit_code != 0:
                raise ValueError(f"{label} PASS requires pytest exit code 0.")
            if outcome is ExecutionOutcome.TEST_FAIL and exit_code != 1:
                raise ValueError(f"{label} TEST_FAIL requires pytest exit code 1.")
        return self

    @property
    def verified(self) -> bool:
        return (
            self.original_outcome is ExecutionOutcome.PASS
            and self.mutant_outcome is ExecutionOutcome.TEST_FAIL
        )


class HardeningResult(BaseModel):
    mutation_id: str
    verdict: Literal["verified", "rejected", "inconclusive"]
    evidence: VerificationEvidence
    explanation: str

    @model_validator(mode="after")
    def verdict_must_match_evidence(self) -> HardeningResult:
        if self.mutation_id != self.evidence.mutation_id:
            raise ValueError(
                "Hardening result mutation_id must match verification evidence."
            )

        if self.evidence.verified:
            expected = "verified"
        elif self.evidence.original_outcome is ExecutionOutcome.TEST_FAIL:
            expected = "rejected"
        else:
            expected = "inconclusive"

        if self.verdict != expected:
            raise ValueError(
                f"Verdict {self.verdict!r} contradicts semantic verification "
                f"evidence; expected {expected!r}."
            )
        return self
