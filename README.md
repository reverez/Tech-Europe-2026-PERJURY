# PERJURY

> **Your CI is green. PERJURY finds what your tests never proved.**

PERJURY is an autonomous adversarial test-hardening agent built for the **{Tech: Europe} Agentic AI Hack — London, 19 September 2026**.

It proposes semantically meaningful mutations, executes them against the real pytest suite in isolated Modal Sandboxes, identifies surviving mutants, asks Gemini for a targeted regression test, and accepts that candidate only after deterministic two-sided execution.

## Correctness invariant

A generated test is **verified** only when:

```text
original + generated test  => PASS
mutant   + generated test  => TEST_FAIL
```

`TEST_FAIL` means pytest ran normally and a test failed. Collection errors, command errors, timeouts, dependency failures, and infrastructure failures never count as proof.

The LLM proposes; execution decides.

## Why the partner technologies are load-bearing

- **Google Gemini** — proposes semantic mutations, explains surviving behavior, and proposes targeted tests.
- **Pydantic + PydanticAI** — validate every model-to-execution contract.
- **Modal** — provides isolated concurrent execution for mutation trials.

Gemini 3.8 Flash is the default configured model through `PERJURY_MODEL`; scripts use the same setting rather than maintaining a separate model choice.

## MVP support contract

The hackathon MVP intentionally supports a narrow, reproducible target:

- Python 3.12.x;
- pytest;
- one repository/workspace snapshot per run;
- an explicit structured install command when needed;
- an explicit structured pytest command;
- roughly 6–10 semantic mutations;
- the bundled refund-policy fixture as the canonical first demo.

**Not promised for the hackathon:** arbitrary dependency discovery, multi-language mutation, GitHub PR automation, databases/services required by arbitrary target repos, formal equivalent-mutant proofs, or general multi-agent orchestration.

Unsupported repository layouts should fail clearly rather than being guessed.

## Core loop

```text
green baseline
     ↓
Gemini proposes semantic mutations
     ↓
Pydantic validation + safe mutation preflight
     ↓
Modal bounded fan-out
     ↓
killed / survived / invalid / timeout / infrastructure error
     ↓
select meaningful survivor
     ↓
Gemini analysis + pytest proposal
     ↓
safe candidate materialization
     ↓
original + candidate  => PASS
mutant   + candidate  => TEST_FAIL
     ↓
deterministic verified hardening result
     ↓
rerun the same mutation batch with the verified test
     ↓
defensible before/after mutation score
```

## Current implementation status

The repository currently contains the protocol scaffold, Gemini/PydanticAI agents, a minimal Modal primitive, FastAPI skeleton, refund fixture, smoke scripts, tests, CI, and the full implementation roadmap.

The production loop is **not yet complete**. The semantic execution foundation (**#8**), provider-independent WorkspaceSpec/baseline layer (**#4**), and deterministic safe mutation applicator (**#5**) are implemented. The sanitized Modal workspace executor (**#6**, live local-vs-Modal parity proven) and bounded concurrent mutation fan-out (**#7**, `perjury/fanout.py`, live 10-Sandbox gate via `scripts/modal_spike.py`) are implemented.

Development is gated by the parent epics:

- [P0 / M0 — deterministic execution foundation](../../issues/1)
- [P1 / M1 — closed autonomous hardening loop](../../issues/2)
- [P2 / M2 — live demo and observability](../../issues/3)
- [P3 / M3 — submission validation and evidence freeze](../../issues/21)

## Repository layout

```text
perjury/
  contracts.py       typed model/execution protocol
  agent.py           Gemini/PydanticAI proposal stages
  modal_runner.py    Modal execution boundary
  workspace.py       WorkspaceSpec snapshot + baseline layer
  mutation.py        isolated exact-anchor mutation applicator
  fanout.py          bounded concurrent mutation fan-out
  verification.py    deterministic verdict logic
  api.py             FastAPI surface
examples/refund/
  refund.py
  test_refund.py
scripts/
  bootstrap.sh       deterministic environment bootstrap
  check.sh           deterministic lint + test gate
  gemini_smoke.py    live Gemini configuration smoke
  modal_spike.py     live Modal 10-mutant fan-out gate (#7)
tests/
docs/
.github/workflows/
  ci.yml             credential-free deterministic CI
```

## Documentation

Read these in order when implementing:

1. [Frozen hackathon specification](docs/SPEC.md)
2. [Target architecture](docs/ARCHITECTURE.md)
3. [Architecture decisions](docs/DECISIONS.md)
4. [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
5. [Development workflow](docs/DEVELOPMENT_WORKFLOW.md)
6. [Test strategy](docs/TEST_STRATEGY.md)
7. [Risk register](docs/RISK_REGISTER.md)
8. [Demo runbook](docs/DEMO_RUNBOOK.md)
9. [Recursive audit checklist](docs/AUDIT_CHECKLIST.md)

## Setup

Requires Python 3.12.x. Direct runtime/dev dependencies are pinned to the versions validated by deterministic CI for the hackathon build.

```bash
git clone https://github.com/reverez/Tech-Europe-2026-PERJURY.git
cd Tech-Europe-2026-PERJURY
bash scripts/bootstrap.sh
```

The bootstrap is deliberately deterministic: it installs the project, creates `.env` if needed, and runs local tests. It does **not** authenticate or call external services.

Run the deterministic gate at any time with:

```bash
bash scripts/check.sh
```

Then configure external services separately:

```bash
# add GOOGLE_API_KEY to .env first
python scripts/gemini_smoke.py

modal setup
python scripts/modal_workspace_smoke.py
python scripts/modal_spike.py
```

This separation prevents provider credentials, quotas, or outages from making every development commit nondeterministic.

## Demo fixture

The bundled refund example intentionally lacks coverage for a premium customer outside the normal refund window. The target demonstration is for PERJURY to expose a semantic mutation affecting that behavior, propose the missing regression test, and prove the candidate through the deterministic two-sided invariant.

A surviving mutant is always described as a **potential test gap**, not automatically as a production bug. Equivalent mutants remain a known limitation of mutation testing.

---

Built from scratch during the {Tech: Europe} Agentic AI Hack on **19 September 2026**.
