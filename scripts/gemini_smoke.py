"""Minimal Gemini + PydanticAI smoke test.

Requires GOOGLE_API_KEY in .env.
"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent


load_dotenv()


class SmokeResult(BaseModel):
    ok: bool
    message: str


def main() -> None:
    agent = Agent(
        "google:gemini-3.8-flash",
        output_type=SmokeResult,
        system_prompt="Return a concise structured health check.",
    )
    result = agent.run_sync("Confirm the PERJURY Gemini connection is working.")
    print(result.output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
