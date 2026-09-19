from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic_ai import Agent

from .contracts import MutationBatch, SurvivorAnalysis, TestProposal


load_dotenv()

MODEL = os.getenv("PERJURY_MODEL", "google:gemini-3.8-flash")

mutation_agent = Agent(
    MODEL,
    output_type=MutationBatch,
    system_prompt=(
        "You are PERJURY's adversarial mutation planner. Propose semantically meaningful, "
        "small mutations that preserve syntax and plausibly expose behavioural assumptions "
        "missing from a Python pytest suite. Never claim a surviving mutation is automatically "
        "a bug; it is only a potential test gap."
    ),
)

survivor_agent = Agent(
    MODEL,
    output_type=SurvivorAnalysis,
    system_prompt=(
        "Analyse one mutation that survived the existing pytest suite. Explain the behavioural "
        "distinction it may expose and whether the mutant could be equivalent in the valid input "
        "domain. Produce a precise test intent."
    ),
)

test_agent = Agent(
    MODEL,
    output_type=TestProposal,
    system_prompt=(
        "Generate one focused pytest regression test for a surviving mutation. The proposal will "
        "be accepted only if deterministic execution proves that it passes on the original "
        "program and fails on the mutant."
    ),
)
