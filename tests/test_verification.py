from perjury.contracts import VerificationEvidence
from perjury.verification import judge_verification


def test_accepts_only_original_pass_mutant_fail() -> None:
    evidence = VerificationEvidence(
        mutation_id="M03",
        original_exit_code=0,
        mutant_exit_code=1,
        original_duration_ms=15,
        mutant_duration_ms=17,
    )

    result = judge_verification(evidence, mutation_id="M03")

    assert result.verdict == "verified"
    assert result.evidence.verified is True


def test_rejects_test_that_breaks_original() -> None:
    evidence = VerificationEvidence(
        mutation_id="M03",
        original_exit_code=1,
        mutant_exit_code=1,
        original_duration_ms=15,
        mutant_duration_ms=17,
    )

    result = judge_verification(evidence, mutation_id="M03")

    assert result.verdict == "rejected"
