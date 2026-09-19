from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MutationStatus(StrEnum):
    KILLED = "killed"
    SURVIVED = "survived"
    INVALID = "invalid"
    TIMEOUT = "timeout"


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
    status: MutationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(ge=0)


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
    original_exit_code: int
    mutant_exit_code: int
    original_stdout: str = ""
    mutant_stdout: str = ""
    original_duration_ms: int = Field(ge=0)
    mutant_duration_ms: int = Field(ge=0)

    @property
    def verified(self) -> bool:
        return self.original_exit_code == 0 and self.mutant_exit_code != 0


class HardeningResult(BaseModel):
    mutation_id: str
    verdict: Literal["verified", "rejected", "inconclusive"]
    evidence: VerificationEvidence
    explanation: str

    @model_validator(mode="after")
    def verdict_must_match_evidence(self) -> "HardeningResult":
        if self.verdict == "verified" and not self.evidence.verified:
            raise ValueError(
                "A hardening result cannot be verified unless the test passes "
                "on the original and fails on the mutant."
            )
        return self
