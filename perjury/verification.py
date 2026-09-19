from .contracts import HardeningResult, VerificationEvidence


def judge_verification(
    evidence: VerificationEvidence,
    *,
    mutation_id: str,
) -> HardeningResult:
    """Deterministically judge a generated test; no LLM is involved here."""
    if evidence.original_exit_code == 0 and evidence.mutant_exit_code != 0:
        return HardeningResult(
            mutation_id=mutation_id,
            verdict="verified",
            evidence=evidence,
            explanation="Generated test passes on original and fails on mutant.",
        )

    if evidence.original_exit_code != 0:
        return HardeningResult(
            mutation_id=mutation_id,
            verdict="rejected",
            evidence=evidence,
            explanation="Generated test breaks the original behaviour.",
        )

    return HardeningResult(
        mutation_id=mutation_id,
        verdict="inconclusive",
        evidence=evidence,
        explanation="Generated test does not distinguish original from mutant.",
    )
