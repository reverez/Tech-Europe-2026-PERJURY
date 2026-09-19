from .contracts import ExecutionOutcome, HardeningResult, VerificationEvidence


def judge_verification(
    evidence: VerificationEvidence,
    *,
    mutation_id: str,
) -> HardeningResult:
    """Deterministically judge a generated test; no LLM is involved here."""
    if evidence.verified:
        return HardeningResult(
            mutation_id=mutation_id,
            verdict="verified",
            evidence=evidence,
            explanation=(
                "Generated test passes on the original and produces a normal "
                "pytest test failure on the mutant."
            ),
        )

    if evidence.original_outcome is ExecutionOutcome.TEST_FAIL:
        return HardeningResult(
            mutation_id=mutation_id,
            verdict="rejected",
            evidence=evidence,
            explanation="Generated test fails against the original behavior.",
        )

    if evidence.original_outcome is not ExecutionOutcome.PASS:
        return HardeningResult(
            mutation_id=mutation_id,
            verdict="inconclusive",
            evidence=evidence,
            explanation="Original-world execution did not complete as a clean pytest pass.",
        )

    return HardeningResult(
        mutation_id=mutation_id,
        verdict="inconclusive",
        evidence=evidence,
        explanation=(
            "Mutant-world execution did not provide the required normal pytest "
            "test failure."
        ),
    )
