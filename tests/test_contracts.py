import pytest
from pydantic import ValidationError

from perjury.contracts import HardeningResult, VerificationEvidence


def test_verified_result_requires_two_sided_evidence() -> None:
    evidence = VerificationEvidence(
        mutation_id="M01",
        original_exit_code=1,
        mutant_exit_code=1,
        original_duration_ms=10,
        mutant_duration_ms=10,
    )

    with pytest.raises(ValidationError):
        HardeningResult(
            mutation_id="M01",
            verdict="verified",
            evidence=evidence,
            explanation="should not validate",
        )
