"""The canonical refund demo mutation batch (deterministic; no model call required).

Includes the demo survivor: removing ``or premium`` is not caught because no existing test
covers a premium customer outside the 30-day window.
"""

from __future__ import annotations

from .contracts import MutationBatch, MutationProposal

REFUND_FILE = "examples/refund/refund.py"
_PREMIUM = "    if days_since_purchase <= 30 or premium:"
_RETURN_PRICE = "        return price\n"

# (description, original, mutated)
_CANONICAL = (
    ("Premium customers lose out-of-window refund", _PREMIUM, "    if days_since_purchase <= 30:"),
    ("Window boundary 30 -> 29", _PREMIUM, "    if days_since_purchase <= 29 or premium:"),
    (
        "Premium no longer sufficient alone",
        _PREMIUM,
        "    if days_since_purchase <= 30 and premium:",
    ),
    ("Refund halved inside the window", _RETURN_PRICE, "        return price / 2\n"),
    ("Out-of-window refund granted in full", "    return 0.0", "    return price"),
    ("Out-of-window refund becomes 1.0", "    return 0.0", "    return 1.0"),
    ("Negative price no longer rejected", "    if price < 0:", "    if price < -1:"),
    ("Inside-window refund inflated", _RETURN_PRICE, "        return price + 1\n"),
)


def canonical_refund_batch() -> MutationBatch:
    return MutationBatch(
        rationale="Canonical refund-policy mutations for the PERJURY demo.",
        mutations=[
            MutationProposal(
                id=f"M{i:02d}",
                file_path=REFUND_FILE,
                description=description,
                hypothesis=description,
                original_snippet=original,
                mutated_snippet=mutated,
            )
            for i, (description, original, mutated) in enumerate(_CANONICAL, start=1)
        ],
    )


class CanonicalRefundPlanner:
    """Deterministic MutationPlanner returning the canonical batch."""

    def propose(self, request: object) -> MutationBatch:
        return canonical_refund_batch()
