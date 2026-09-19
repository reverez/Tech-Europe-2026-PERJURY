"""Live closed-loop smoke (NOT part of deterministic CI): Gemini + Modal through run_perjury.

Requires GOOGLE_API_KEY (loaded from .env) and `modal setup`. Uses the sanitized refund snapshot;
the baseline runs locally, every mutation/candidate world runs in Modal Sandboxes.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import modal
from dotenv import load_dotenv

from perjury.analysis import GeminiSurvivorAnalyzer
from perjury.canonical import CanonicalRefundPlanner
from perjury.contracts import SurvivorAnalysis, TestProposal
from perjury.generation import GeminiTestGenerator
from perjury.modal_runner import ModalWorkspaceExecutor, _get_app
from perjury.orchestrator import RunConfig, RunStage, run_perjury
from perjury.planning import GeminiMutationPlanner, PlanningConfig
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock-models",
        action="store_true",
        help="use deterministic mock model outputs (Modal is still live); no Gemini key needed",
    )
    args = parser.parse_args()
    load_dotenv()
    if args.mock_models:
        planner, analyzer, generator = CanonicalRefundPlanner(), MockAnalyzer(), MockGenerator()
    else:
        if not os.getenv("GOOGLE_API_KEY"):
            raise SystemExit(
                "ERROR: GOOGLE_API_KEY is missing; add it to .env or use --mock-models."
            )
        planner, analyzer, generator = (
            GeminiMutationPlanner(),
            GeminiSurvivorAnalyzer(),
            GeminiTestGenerator(),
        )
    result = run_perjury(
        refund_workspace_spec(str(ROOT)),
        planner=planner,
        analyzer=analyzer,
        generator=generator,
        executor=ModalWorkspaceExecutor(),
        baseline_executor=LocalPytestExecutor(),
        config=RunConfig(planning=PlanningConfig.from_env()),
        on_event=lambda e: print(
            f"[{e.at_ms:>6}ms] {e.seq:>3} {e.type.value} {e.mutation_id or ''}"
        ),
    )
    print(f"\nstate={result.state.value} reason={result.reason.value} total={result.total_ms}ms")
    print(result.message)
    for stage in result.stages:
        print(f"  {stage.stage.value:<20} {stage.duration_ms:>7}ms {stage.status}")
    if result.rescore:
        c = result.rescore.comparison
        print(f"score {c.before.score} -> {c.after.score} delta={c.delta} ({c.direction})")
    leaked = sum(1 for _ in modal.Sandbox.list(app_id=_get_app().app_id))
    print(f"running sandboxes after run: {leaked}")
    if result.state is not RunStage.VERIFIED or leaked:
        raise SystemExit(f"FAIL: closed loop ended {result.state.value}/{result.reason.value}")
    mode = "mocked models, live Modal" if args.mock_models else "live Gemini + Modal"
    print(f"PASS: closed loop VERIFIED with same-batch re-score ({mode})")


if __name__ == "__main__":
    main()
