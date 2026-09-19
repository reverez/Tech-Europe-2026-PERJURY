"""Deterministic mock model outputs shared by the smoke/serve scripts (not used in production)."""

from __future__ import annotations

from perjury.contracts import SurvivorAnalysis, TestProposal


class MockAnalyzer:
    def analyze(self, request):
        return SurvivorAnalysis(
            mutation_id=request.mutation_id,
            behavioural_gap="No test covers premium customers outside the 30-day window.",
            test_intent="Premium customers past day 30 keep a full refund.",
            reasoning="Removing 'or premium' only changes premium customers after day 30.",
        )


class MockGenerator:
    def generate(self, request):
        return TestProposal(
            mutation_id=request.mutation_id,
            test_name="test_premium_customer_outside_window_still_gets_refund",
            target_file="examples/refund/test_refund.py",
            test_code=(
                "from examples.refund.refund import calculate_refund\n\n\n"
                "def test_premium_customer_outside_window_still_gets_refund() -> None:\n"
                "    assert calculate_refund(100.0, 45, premium=True) == 100.0\n"
            ),
            explanation="Pins the premium out-of-window refund rule.",
        )
