# PERJURY — Target Architecture

This document describes the target architecture for the hackathon build. It is intentionally narrower than a production mutation-testing platform: Python + pytest, one configured workspace at a time, 6–10 semantic mutations, Modal isolation, Gemini/PydanticAI proposal stages, and deterministic verification.

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
mutant   + generated test  => TEST_FAIL
```

Infrastructure errors, collection errors, dependency failures, timeouts, and other invalid executions must never be interpreted as a verified behavioral distinction.

## 2. End-to-end data flow

```text
Run request
   |
   v
WorkspaceSpec + immutable source snapshot
   |
   v
Baseline runner
   |---- non-PASS -------------------------------> terminal failure
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
   +--> TEST_FAIL => killed
   +--> PASS      => survived --------------------+
   +--> INVALID / TIMEOUT / INFRA_ERROR           |
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
                                    verified candidate?
                                      | yes
                                      v
                             re-score same mutation batch
                                      v
                         final run result + before/after score
                                      v
                            Evidence/events -> API -> UI
```

## 3. Core contracts

### WorkspaceSpec
Target shape for #4:
- run/workspace identity;
- immutable source root/snapshot;
- Python/runtime assumption;
- optional structured install argv;
- structured pytest argv;
- working directory;
- execution timeout;
- mutation concurrency limit;
- mutable implementation-path allowlist;
- test/context path allowlist;
- sanitized snapshot exclusions;
- max captured stdout/stderr bytes and truncation policy;
- maximum snapshot file count/total bytes;
- explicit network policy, defaulting to blocked;
- explicit non-secret environment mapping/allowlist; target execution never inherits arbitrary host environment.

Commands are structured argv-like data, not interpolated shell strings.

### Provider boundary
Baseline execution records the sanitized manifest before and after the command and becomes mutation-ready only when the source identity is unchanged.

Workspace/baseline semantics are provider-independent. The core accepts an executor interface that deterministic tests can fake/inject; the production Modal implementation is supplied later by #6. Baseline and mutant paths share the semantic execution contract without making #4 depend on live Modal.

### Execution outcome
The shared semantic execution taxonomy from #8:

- **PASS** — pytest completed and selected tests passed.
- **TEST_FAIL** — pytest completed normally and tests failed.
- **INVALID** — mutation/test/command is structurally unusable, including collection/usage/no-test cases as specified.
- **TIMEOUT** — allowed execution time was exceeded.
- **INFRA_ERROR** — Sandbox/bootstrap/dependency/runtime/provider failure.

Primary command outcome is recorded separately from cleanup/teardown problems so a successful or test-failing pytest process is not silently rewritten by a later cleanup failure.

Raw exit code, stdout, and stderr remain evidence fields but do not replace the semantic outcome.

### Mutation status projection
For a mutant trial:
- PASS → **survived**
- TEST_FAIL → **killed**
- INVALID/TIMEOUT/INFRA_ERROR remain explicit and are excluded from the mutation-score denominator.

### VerificationEvidence
Verification records two semantic executions: original world and mutant world. `verified` is derivable only from original PASS + mutant TEST_FAIL.

## 4. Components

| Component | Responsibility | Must not do |
| --- | --- | --- |
| `contracts.py` | typed workspace, execution, proposal, run, event, and verification protocol | encode contradictory states |
| workspace layer | immutable source snapshots and safe target paths | modify the source repository in place |
| context packer | bounded implementation/test context for Gemini | include secrets or arbitrary binary/generated files |
| `agent.py` | Gemini/PydanticAI proposal stages | make final correctness decisions |
| mutation applicator | exact-anchor safe mutation + diff evidence | guess which source occurrence to edit |
| `modal_runner.py` | lazy Modal initialization, workspace execution, evidence capture | perform network lookup on import or treat every non-zero exit as TEST_FAIL |
| orchestrator | typed stage machine and evidence/event emission | mix UI behavior into core execution |
| `verification.py` | deterministic two-world judgment | consult an LLM |
| `api.py` | run creation/state/result + SSE events | duplicate orchestration logic |
| demo UI | project real run state | present fabricated/canned success as live evidence |

## 5. Runtime initialization rule

Importing PERJURY modules must be deterministic and offline-safe.

External clients/apps are initialized only when a live operation or explicit preflight begins. In particular, Modal lookup/creation must not occur as an import-time side effect. This keeps deterministic tests, static tooling, API startup checks, and mocked integration tests independent of external credentials.

## 6. Isolation and mutation safety

Every mutation/test trial operates on its own isolated workspace.

Required controls:
1. repository-relative paths only;
2. resolve paths and reject absolute paths, traversal, and symlink escape;
3. mutations may target only the mutable implementation allowlist, never tests/context/excluded files;
4. snapshot upload is manifest-driven and excludes secrets/runtime directories rather than copying the repository blindly;
5. mutation targets/results are UTF-8 text without NUL bytes, and the exact original snippet anchor must match once;
6. source snapshot is immutable during a run;
7. generated tests are created as new files in an allowed test directory inside an isolated candidate workspace; existing tests are not overwritten/appended;
8. every applied change has a renderable diff;
9. stdout/stderr capture is bounded and marks truncation;
10. snapshot file-count/byte limits reject oversized workspaces before upload;
11. pytest Sandboxes block outbound network by default; any opt-in is explicit in WorkspaceSpec/evidence;
12. execution receives only the explicit sanitized environment from WorkspaceSpec;
13. Sandboxes terminate on success, failure, exception, and timeout;
14. cleanup/teardown errors are retained separately from the primary pytest outcome;
15. unsupported target setup fails explicitly rather than being guessed.

## 7. Concurrency model

The hackathon target is a bounded fan-out of roughly 6–10 mutations.

The executor must:
- preserve mutation ID ↔ result association independent of completion order;
- enforce per-mutation timeouts;
- record wall-clock and per-job duration;
- return exactly one terminal result per accepted mutation;
- clean up every Sandbox;
- avoid unbounded retries during the live demo.

Retries, if introduced for agent generation, must be bounded and visible in evidence.

## 8. Run state and events

#15 owns the typed run state machine used by the API, evidence layer, and UI.

At minimum the run should distinguish:
- created/preflight;
- baseline;
- planning;
- mutation execution;
- survivor analysis;
- test generation/materialization;
- verification;
- terminal verified/rejected/inconclusive/failed state.

State transitions are monotonic for one run. Events carry the run ID and enough identity to correlate mutation-level updates. Consumers must not reconstruct authoritative state from UI timing.

## 9. Evidence model

Each run should retain enough structured evidence to reconstruct what happened:

- run ID and code revision when available;
- stage timings;
- WorkspaceSpec minus secrets;
- baseline command/result;
- selected source/test context metadata;
- raw validated `MutationBatch`;
- applied mutation diffs;
- bounded sandbox stdout/stderr, truncation metadata, and semantic execution outcomes;
- selected survivor and `SurvivorAnalysis`;
- generated `TestProposal`;
- original-vs-mutant verification evidence;
- final `HardeningResult`;
- mutation score and excluded invalid/error counts.

Secrets and credentials must never be included in exported evidence. Sanitized bundles are persisted under the gitignored `.perjury/runs/<run_id>/evidence.json` path for the hackathon; no database is required.

## 10. API and demo boundary

The core pipeline must be callable without the UI.

The default demo transport is same-origin SSE because progress is primarily server-to-client. The API uses an in-memory single-process run registry and accepts only one active hardening run for the MVP. The API exposes run creation, state/result retrieval, event streaming, and health information; explicit external-service preflight lives in `scripts/preflight.py`.

The MVP UI is zero-build local HTML/CSS/vanilla JavaScript served by FastAPI; it does not require a Node toolchain or external CDN.

The UI distinguishes:
1. **executed fact** — pytest/Modal results and deterministic verification;
2. **model proposal** — Gemini mutation/analysis/test text;
3. **derived metric** — mutation score, durations, counts.

A previously captured real evidence bundle may be shown as a clearly labelled recorded backup run, never as live execution.

## 11. Development and validation boundaries

- Default local/CI checks are deterministic and credential-free.
- Modal and Gemini have explicit smoke layers.
- Full live rehearsal is a milestone gate, not a per-commit test.
- External-service failure never justifies weakening verification semantics.
- A merge changing shared contracts must update tests and consumers together.

See:
- [DECISIONS.md](./DECISIONS.md)
- [DEVELOPMENT_WORKFLOW.md](./DEVELOPMENT_WORKFLOW.md)
- [TEST_STRATEGY.md](./TEST_STRATEGY.md)
- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)

## 12. Implementation map

- **M0 / P0 — deterministic execution foundation:** #1, #22, #4–#8
- **M1 / P1 — closed autonomous loop:** #2, #9–#14, #25, #15–#16
- **M2 / P2 — live demo and observability:** #3, #17–#20
- **M3 / P3 — submission validation/evidence freeze:** #21, #24, #23
