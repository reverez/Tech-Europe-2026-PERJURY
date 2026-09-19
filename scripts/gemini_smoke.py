"""Minimal Gemini + PydanticAI smoke test.

Requires GOOGLE_API_KEY in .env and uses the same PERJURY_MODEL setting as the application.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent


load_dotenv()


class SmokeResult(BaseModel):
    ok: bool
    message: str


def main() -> None:
    if not os.getenv("GOOGLE_API_KEY"):
        raise SystemExit(
            "ERROR: GOOGLE_API_KEY is missing. Add it to .env before the Gemini smoke test."
        )

    model = os.getenv("PERJURY_MODEL", "google:gemini-3.8-flash")
    agent = Agent(
        model,
        output_type=SmokeResult,
        system_prompt="Return a concise structured health check.",
    )
    result = agent.run_sync("Confirm the PERJURY Gemini connection is working.")
    print(f"model={model}")
    print(result.output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
