# PERJURY

> **Your CI is green. PERJURY finds what your tests never proved.**

PERJURY is an autonomous adversarial test-hardening agent built for the **{Tech: Europe} Agentic AI Hack — London, 19 September 2026**.

It attacks a Python/pytest codebase with semantically meaningful mutations, executes those mutations concurrently in isolated Modal Sandboxes, identifies surviving mutants, asks Gemini to generate targeted regression tests, and accepts a generated test **only** after deterministic two-sided verification:

1. the new test **passes on the original program**, and
2. the same test **fails on the surviving mutant**.

That invariant keeps the LLM out of the final correctness decision.

## Why these partner technologies are load-bearing

- **Google Gemini** — proposes semantic mutations, explains surviving behaviours, and generates targeted tests.
- **Pydantic + PydanticAI** — define and validate every model-to-execution contract.
- **Modal** — runs mutation trials concurrently in isolated Sandboxes.

## Core loop

```text
baseline pytest
     ↓
Gemini proposes N semantic mutations
     ↓
Pydantic validates MutationBatch
     ↓
Modal fans out N isolated pytest runs
     ↓
killed / survived / invalid / timeout
     ↓
Gemini analyses survivors
     ↓
Gemini proposes targeted regression test
     ↓
two-sided verification
  original + test → PASS
  mutant   + test → FAIL
     ↓
verified hardening result
```

## Hackathon scope

**In:** Python repositories, pytest, 6–10 mutations per run, one reliable live demo.

**Out today:** multi-language support, arbitrary dependency resolution, GitHub PR automation, databases, formal equivalent-mutant proofs, multi-agent orchestration.

## Repository layout

```text
perjury/
  contracts.py       # typed protocol crossing the model/execution boundary
  agent.py           # PydanticAI/Gemini orchestration
  modal_runner.py    # Modal sandbox execution boundary
  verification.py    # deterministic two-sided acceptance invariant
  api.py             # FastAPI surface
examples/refund/
  refund.py
  test_refund.py
scripts/
  modal_spike.py     # first technical gate: concurrent sandbox + pytest
tests/
  test_contracts.py
  test_verification.py
```

## Project documentation

- [Frozen hackathon specification](docs/SPEC.md)
- [Target architecture](docs/ARCHITECTURE.md)
- [Implementation plan and milestone gates](docs/IMPLEMENTATION_PLAN.md)
- GitHub epics: [P0 / M0](../../issues/1), [P1 / M1](../../issues/2), [P2 / M2](../../issues/3), [P3 / M3](../../issues/21)

## Setup

Requires Python 3.12+.

```bash
git clone https://github.com/reverez/Tech-Europe-2026-PERJURY.git
cd Tech-Europe-2026-PERJURY

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
```

Authenticate Modal:

```bash
modal setup
```

Then add your Gemini API key to `.env`.

## First gate — Modal concurrency spike

Before building the UI, prove the execution primitive:

```bash
python scripts/modal_spike.py
```

Target: launch roughly 10 isolated pytest executions concurrently and collect structured results quickly and reliably. If this primitive is unstable, the project pivots before any time is spent polishing the demo.

## Local checks

```bash
pytest
uvicorn perjury.api:app --reload
```

## Correctness rule

A generated test is never accepted because Gemini says it is good.

```text
PASS(original + generated test)
AND
FAIL(mutant + generated test)
= VERIFIED TEST
```

A surviving mutant is reported as a **potential test gap**, not automatically as a bug: equivalent mutants remain a known mutation-testing limitation.

## Demo target

The first demo uses a small refund-policy module with a superficially strong test suite. PERJURY creates semantic attacks such as deleting the premium-customer exception, exposes the surviving behaviour, generates the missing regression test, and proves it through two-sided execution.

---

Built from scratch during the {Tech: Europe} Agentic AI Hack on **19 September 2026**.
