"""Launch the PERJURY demo server (API + UI at http://HOST:PORT/demo).

    python scripts/serve_demo.py                 # live: Gemini + Modal (needs GOOGLE_API_KEY, modal setup)
    python scripts/serve_demo.py --mock-models   # deterministic mock model outputs; every execution
                                                 # (baseline, mutants, worlds, re-score) is REAL pytest

The mock-model mode is for rehearsal and CI-style smokes; only the model outputs are canned.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from _mock_models import MockAnalyzer, MockGenerator
from dotenv import load_dotenv

from perjury.api import _commit_sha, app, create_app
from perjury.canonical import CanonicalRefundPlanner
from perjury.orchestrator import run_perjury
from perjury.runs import RunManager
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec

ROOT = Path(__file__).resolve().parents[1]


def mock_app():
    def execute(run_id, on_event):
        executor = LocalPytestExecutor()
        return run_perjury(
            refund_workspace_spec(str(ROOT)),
            planner=CanonicalRefundPlanner(),
            analyzer=MockAnalyzer(),
            generator=MockGenerator(),
            executor=executor,
            run_id=run_id,
            on_event=on_event,
            commit_sha=_commit_sha(),
        )

    return create_app(RunManager(execute))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-models", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    load_dotenv()
    if args.mock_models:
        print("MOCK MODELS: model outputs are canned; all executions are real local pytest runs.")
        target = mock_app()
    else:
        if not os.getenv("GOOGLE_API_KEY"):
            print("WARNING: GOOGLE_API_KEY is not set; live runs will fail at planning.")
        target = app
    print(f"Open http://{args.host}:{args.port}/demo")
    uvicorn.run(target, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
