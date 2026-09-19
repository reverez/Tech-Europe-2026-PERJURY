# PERJURY — Target Architecture

This document describes the implementation architecture for the hackathon build. It is intentionally narrower than a production mutation-testing platform: Python + pytest, one repository at a time, 6–10 semantic mutations, Modal isolation, Gemini/PydanticAI planning, and deterministic verification.

## 1. Architectural principle

PERJURY separates **probabilistic proposal** from **deterministic proof**.

Gemini may:
- propose semantic mutations;
- explain a surviving mutant;
- propose a regression test.

Gemini may **not** decide whether a regression test is correct.

A candidate is accepted only when execution proves:

```text
original + generated test  => PASS
mutant   + generated test  => TEST FAILURE
```

Infrastructure errors, collection errors, dependency failures, timeouts, and other invalid executions must never be interpreted as a verified behavioural distinction.

## 2. End-to-end data flow

```text
Run request
   |
   v
Baseline runner
   |---- baseline not green ----------------------> terminal failure
   v
Repository context packer
   v
Gemini mutation planner
   v
Pydantic validation + mutation preflight
   v
Safe mutation applicator
   v
Modal bounded fan-out
   |
   +--> killed
   +--> survived --------------------+
   +--> invalid                       |
   +--> timeout                       |
                                      v
                              Survivor selection
                                      v
                              Gemini survivor analysis
                                      v
                              Gemini pytest proposal
                                      v
                              Safe test materializer
                                      v
                         +------------+-------------+
                         |                          |
                         v                          v
                  original + test             mutant + test
                         |                          |
                         +------------+-------------+
                                      v
                             Deterministic judge
                                      v
                              Hardening result
                                      v
                         Evidence store / API / UI
```

## 3. Components

| Component | Responsibility | Must not do |
| --- | --- | --- |
| `contracts.py` | Typed protocol for proposals, execution, analysis, verification, and run state | Hide contradictory states |
| workspace layer | Represent repository/test command and create isolated copies | Modify the source repository in place |
| context packer | Select implementation/tests for Gemini within a bounded context budget | Include secrets or arbitrary binary/generated files |
| `agent.py` | Gemini/PydanticAI proposal stages | Make final correctness decisions |
| mutation applicator | Validate path/snippet anchors and apply one mutation to one isolated workspace | Guess which occurrence to edit |
| `modal_runner.py` | Create Sandboxes, materialize workspaces, execute commands, collect evidence | Treat every non-zero exit code as a valid test failure |
| orchestrator | Drive the stage machine and preserve evidence | Mix UI concerns into core execution |
| `verification.py` | Deterministically judge original-vs-mutant evidence | Consult an LLM |
| `api.py` | Start runs, expose state/results, stream events | Reimplement business logic |
| demo UI | Visualize baseline, fan-out, survivor, generated test, and proof | Display canned success data as if it were executed |

## 4. Execution semantics

The implementation must distinguish at least these semantic outcomes even if the exact internal enum evolves:

- **PASS** — command completed and the selected pytest suite passed.
- **TEST_FAIL** — pytest ran correctly and one or more tests failed.
- **INVALID** — mutation/test could not be collected or was structurally unusable.
- **TIMEOUT** — execution exceeded its allowed time.
- **INFRA_ERROR** — Sandbox/bootstrap/dependency/runtime failure.

Mutation presentation can still map valid executions to the familiar mutation-testing vocabulary:

- PASS on mutant → **survived**
- TEST_FAIL on mutant → **killed**

But the verification layer must operate on the richer execution meaning. A mutant process that crashes because dependencies are missing is not a killed mutant and cannot verify a generated test.

## 5. Isolation and mutation safety

Every mutation/test trial operates on its own isolated workspace.

Required controls:
1. repository-relative paths only;
2. reject absolute paths and traversal;
3. exact snippet anchor must match once;
4. original repository is immutable during a run;
5. generated tests are written only to an isolated workspace;
6. every applied change has a renderable diff;
7. Sandboxes terminate on success, failure, and timeout.

## 6. Concurrency model

The hackathon target is a bounded fan-out of roughly 6–10 mutations.

The executor must:
- preserve mutation ID ↔ result association independent of completion order;
- enforce per-mutation timeouts;
- record wall-clock and per-job duration;
- return exactly one terminal result per accepted mutation;
- clean up every Sandbox;
- avoid unbounded retries during the live demo.

## 7. Evidence model

Each run should retain enough structured evidence to reconstruct what happened:

- run ID and stage timings;
- baseline command/result;
- selected source/test context metadata;
- raw validated `MutationBatch`;
- applied mutation diff;
- sandbox stdout/stderr and execution classification;
- selected survivor and `SurvivorAnalysis`;
- generated `TestProposal`;
- original-vs-mutant verification evidence;
- final `HardeningResult`;
- mutation score before/after where meaningful.

Secrets and credentials must never be included in exported evidence.

## 8. Demo/API boundary

The core pipeline must be callable without the UI. The FastAPI layer should expose run creation/status and stream stage events to the demo surface.

The demo is a projection of real run state. It should make three different kinds of information visually distinct:

1. **executed fact** — pytest/Modal results and deterministic verification;
2. **model proposal** — Gemini mutation/analysis/test text;
3. **derived metric** — mutation score, durations, counts.

## 9. Current implementation map

The repository already contains the protocol skeleton, three Gemini/PydanticAI agents, a minimal Modal execution primitive, a deterministic verifier, FastAPI health surface, refund fixture, and initial tests.

The implementation backlog is tracked by:

- **M0 / P0 — Execution foundation:** #1, #4–#8
- **M1 / P1 — Closed autonomous loop:** #2, #9–#16
- **M2 / P2 — Live demo and observability:** #3, #17–#20
- **M3 / P3 — Submission hardening:** #21–#23

See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) for sequencing and exit gates.
