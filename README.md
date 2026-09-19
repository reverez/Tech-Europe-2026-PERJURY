# PERJURY

> **Your CI is green. PERJURY finds what your tests never proved.**

**Autonomous adversarial test hardening.** Built for the **{Tech: Europe} Agentic AI Hack — London, 19 September 2026**.

PERJURY attacks a green pytest suite with semantically meaningful mutations, runs every mutant against the real suite in isolated Modal Sandboxes, picks a mutant the tests failed to catch, has Gemini write a targeted regression test, and accepts that test **only** after deterministic two-world execution proves it. It then re-runs the *same* mutation batch with the new test to report a defensible before/after mutation score.

**The model proposes. Execution proves.**

## Correctness invariant

A generated test is **VERIFIED** only when:

```text
original + generated test  => PASS
mutant   + generated test  => TEST_FAIL
```

`TEST_FAIL` means pytest ran normally and a test failed. Collection errors, command errors, timeouts, dependency failures and infrastructure failures never count as proof. The score is computed from killed + survived outcomes only; invalid/timeout/infra outcomes stay visible but are excluded, and a score with no valid outcome is reported as unavailable, never 0%.

## How it works

```text
green baseline (pytest PASS on the real suite)
  → Gemini proposes 6–10 semantic mutations        (MODEL, validated by Pydantic)
  → safe exact-anchor application + compile check   (deterministic)
  → Modal bounded fan-out, one Sandbox per mutant   (FACT: killed / survived / invalid / timeout / infra)
  → survivor selection + Gemini analysis            (MODEL: "potential test gap", never "bug")
  → Gemini writes one pytest candidate              (MODEL)
  → safe create-new materialization + collection    (deterministic)
  → two worlds from the same snapshot:
        original + candidate => PASS
        mutant   + candidate => TEST_FAIL           (FACT)
  → VERIFIED
  → same validated batch re-run with the candidate  (FACT) → before/after score (DERIVED)
  → sanitized evidence bundle .perjury/runs/<run_id>/evidence.json
```

One typed entrypoint (`perjury/orchestrator.py: run_perjury`) drives every stage with monotonic, correlated events. A same-origin FastAPI API streams them over SSE to a zero-build dashboard at `/demo`.

### Partner technologies, and what each one is trusted with

| Technology | Role | Trusted to decide? |
| --- | --- | --- |
| **Google Gemini** (`PERJURY_MODEL`, default `google:gemini-3.8-flash`) | semantic mutation proposals, survivor analysis, regression-test generation | **No**, proposals only |
| **Pydantic / PydanticAI** | typed boundaries that turn probabilistic model output into validated, deterministic contracts | validates shape, not truth |
| **Modal** | isolated, network-blocked, concurrent Sandbox execution of every mutant and both verification worlds | runs code, no judgement |
| **pytest / execution** | the final truth: PASS vs TEST_FAIL decides every verdict and score | **Yes** |

## Evidence: three separate categories

**Validated implementation SHA: `1429b6a47b0edd883807f74c631077405b2707b7`** (`dev/integration`). Every commit after it changes documentation only; a later change to executable code would require re-running preflight and the rehearsal (see the [runbook](docs/DEMO_RUNBOOK.md), section 5). Backend logic is identical to the earlier validated `72985a0`: the changes between them are UI presentation, docs, browser-smoke and bootstrap text only.

The categories below are **different runs over different mutation batches**. Do not combine or compare their numbers.

**1. Canonical reproducible rehearsal** (fixed 8-mutant refund batch, mocked model boundaries, **real Modal execution**):
- first pass 5 killed / 3 survived / 0 excluded → **0.625**
- M01 (`or premium` removed) selected; candidate PASS on original, TEST_FAIL on mutant → VERIFIED
- same-batch re-score 6 killed / 2 survived → **0.750** (+12.5 pts)
- ~18–21 s end to end; reproduce with `python scripts/closed_loop_smoke.py --mock-models`

**2. Fully live Gemini + Modal, VERIFIED** (Gemini proposed its own batch; run on `72985a0`, backend-identical to the validated SHA):
- run `run-2e7df7037679`, **VERIFIED** in **54.067 s**
- same-batch live re-score **0.000 → 0.125**
- evidence bundle `sha256:0b29c589ff93…` (80,838 B, complete), 0 leaked Sandboxes

**3. Fully live Gemini + Modal on the validated SHA `1429b6a`, safe rejections.** Two further live runs ended **rejected / `candidate_failed_original`** with 0 leaked Sandboxes: Gemini's generated test failed on the *original* code, so the deterministic judge refused it and no improvement was claimed. This is the invariant working, not a defect:
- `run-b3705a55cea6`, 47.655 s, evidence `sha256:3c5b32c7df26…`
- `run-5d3839cf1daa`, 51.958 s, evidence `sha256:3b127030c415…`

Live test generation is nondeterministic: of these three live runs, one verified and two were correctly rejected. The reproducible path is category 1.

## Setup (one canonical path)

Requires Python 3.12.x and git. Dependencies are pinned.

```bash
git clone https://github.com/reverez/Tech-Europe-2026-PERJURY.git
cd Tech-Europe-2026-PERJURY
bash scripts/bootstrap.sh        # venv + pinned install + .env from .env.example + deterministic gate
source .venv/bin/activate
```

Bootstrap never calls external services. For the live path, add `GOOGLE_API_KEY` to `.env` and run `modal setup`.

## Run it

```bash
python scripts/preflight.py                        # readiness by subsystem; exit 0 only if all pass
python -m uvicorn perjury.api:app --port 8000      # live Gemini + Modal → http://127.0.0.1:8000/demo
```

Press **Run PERJURY**. Other entry points:

```bash
python scripts/serve_demo.py --mock-models         # rehearsal: canned model outputs, real pytest, no credentials
python scripts/closed_loop_smoke.py                # timed live rehearsal (fails if > 110 s); saves evidence
bash scripts/check.sh                              # deterministic gate (ruff + 463 tests), no credentials
python scripts/browser_smoke.py                    # real-browser smoke of /demo against the real API
```

`/demo?adapter=mock` is a separate replay for UI development only. It always shows a permanent **SIMULATED** banner and is not evidence.

## API

| Method | Path | |
| --- | --- | --- |
| POST | `/api/runs` | start the run (202; typed 409 if one is active) |
| GET | `/api/runs/{id}` | authoritative typed snapshot |
| GET | `/api/runs/{id}/events` | ordered SSE events (resume with `Last-Event-ID` or `?after=`) |
| GET | `/api/runs/{id}/evidence` | the sanitized evidence bundle |
| GET | `/health` | process health |

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md).

## Scope (hackathon MVP)

Python 3.12 + pytest, one workspace snapshot per run, explicit structured install/test commands, 6–10 semantic mutations, the bundled refund-policy fixture as the canonical demo. **Not promised:** arbitrary dependency discovery, other languages, PR automation, services required by arbitrary repos, or formal equivalent-mutant proofs. A surviving mutant is always a **potential test gap**, not a confirmed bug.

## Repository layout

```text
perjury/
  orchestrator.py    run_perjury: the single typed closed-loop entrypoint + run state machine
  contracts.py       typed model/execution protocol
  agent.py           Gemini/PydanticAI agents (constructed lazily)
  context.py         bounded source/test context with untrusted-content delimiting
  planning.py        mutation planning, validation, dedup, bounded replenishment
  mutation.py        isolated exact-anchor mutation applicator
  workspace.py       WorkspaceSpec, sanitized snapshot manifests, baseline
  modal_runner.py    Modal Sandbox executor (single verified snapshot archive per Sandbox)
  fanout.py          bounded concurrent fan-out
  execution.py       first-pass execution + authoritative score
  analysis.py        survivor analysis + hardening-target selection
  generation.py      test-generation boundary
  candidate.py       safe create-new candidate materialization + collection preflight
  hardening.py       two-world verification
  verification.py    deterministic verdict
  rescore.py         same-batch before/after comparison
  evidence.py        sanitized, atomic, hashed evidence bundle
  runs.py / api.py   in-memory run registry, typed API + SSE
  preflight.py       subsystem-grouped live readiness checks
  ui/                zero-build dashboard (/demo): HTML/CSS/vanilla JS, no CDN
examples/refund/     canonical fixture (premium-customer gap)
scripts/             bootstrap, check, preflight, serve_demo, smokes
tests/               deterministic suite (no credentials, no network)
docs/                spec, architecture, decisions, runbook, API/UI contracts
```

## Documentation

1. [Specification](docs/SPEC.md)
2. [Architecture](docs/ARCHITECTURE.md)
3. [Architecture decisions](docs/DECISIONS.md)
4. [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
5. [Development workflow](docs/DEVELOPMENT_WORKFLOW.md)
6. [Test strategy](docs/TEST_STRATEGY.md)
7. [Risk register](docs/RISK_REGISTER.md)
8. [Demo runbook](docs/DEMO_RUNBOOK.md)
9. [Recursive audit checklist](docs/AUDIT_CHECKLIST.md)

Interface contracts: [API](docs/API_CONTRACT.md) · [UI](docs/UI_CONTRACT.md)

---

Built from scratch during the {Tech: Europe} Agentic AI Hack on **19 September 2026**.
