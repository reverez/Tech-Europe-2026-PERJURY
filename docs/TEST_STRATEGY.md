# PERJURY — Test Strategy

PERJURY deliberately separates deterministic correctness tests from live external-service tests. This keeps iteration fast while still validating Gemini and Modal at explicit gates.

## 1. Test layers

| Layer | Purpose | External credentials | Runs by default |
| --- | --- | ---: | ---: |
| L0 Unit | contracts, path safety, exit mapping, verdict matrix | No | Yes |
| L1 Deterministic integration | orchestration with mocked agent/executor boundaries | No | Yes |
| L2 Modal smoke | real workspace materialization, cleanup, concurrency | Modal | No |
| L3 Gemini smoke | configured model + Pydantic structured output | Google | No |
| L4 Full rehearsal | real Gemini + Modal + API/UI happy path | Both | Milestone/demo gate |

#22 establishes L0/L1 as the default local/CI gate.

## 2. Pytest exit semantics

Do not treat every non-zero pytest exit as a test failure.

Pytest process semantics used by PERJURY:

| Exit | Meaning | PERJURY semantic outcome |
| ---: | --- | --- |
| 0 | tests passed | PASS |
| 1 | tests ran and failed | TEST_FAIL |
| 2 | interrupted | INVALID |
| 3 | internal error | INFRA_ERROR |
| 4 | command-line usage error | INVALID |
| 5 | no tests collected | INVALID |

Timeout is represented independently as TIMEOUT.

Only **PASS(original)** plus **TEST_FAIL(mutant)** can verify a generated regression test.

## 3. Minimum deterministic coverage by subsystem

### Contracts
- valid serialization round trips;
- contradictory states rejected;
- required IDs/paths/statuses validated.

### Workspace/path safety
- source manifest identity before/after baseline execution;
- normal repository-relative path;
- absolute path rejection;
- `../` traversal rejection;
- symlink escape rejection;
- implementation-only mutation allowlist;
- tests/context paths forbidden as mutation targets;
- secret/runtime directories absent from snapshot manifest;
- snapshot file-count/byte limits;
- network policy defaults to blocked;
- explicit target environment does not inherit arbitrary host variables;
- missing/ambiguous snippet anchor rejection;
- original workspace remains unchanged.

### Execution mapping
- real provider-independent refund baseline passes locally;
- real local TEST_FAIL and no-tests INVALID classifications;
- pytest 0/1/2/3/4/5;
- timeout;
- bootstrap/install/provider failure;
- bounded stdout/stderr capture;
- truncation metadata propagation.

### Mutation pipeline
- duplicate/no-op proposal rejection;
- mutation score excludes invalid/timeouts;
- zero survivors terminal path;
- possibly equivalent survivor path.

### Generated test
- safe contextual target path;
- generated candidate uses a new unique file and never overwrites/appends existing tests;
- invalid syntax;
- collection failure;
- original regression rejection;
- verified original-pass/mutant-test-fail case;
- mutant infra failure never verifies.

### Re-score
- same MutationBatch identity before/after;
- verified selected mutant becomes killed or run is flagged inconsistent;
- invalid/timeout/infra exclusions do not enter the denominator;
- exact post-score is not computed from assumptions about previously killed mutants.

### Orchestrator
- happy path;
- baseline failure stops the run;
- planning failure stops before execution;
- no survivor terminal state;
- rejected test terminal state;
- verified terminal state;
- evidence and timing retained at each transition.

### API
- create run;
- query run;
- event ordering;
- terminal error response;
- unknown run ID.

## 4. Deterministic E2E fixture

The bundled refund example is the canonical integration fixture.

The initial tests intentionally omit the premium-customer-outside-window case. A known semantic mutation can remove the premium exception and survive the initial suite. The deterministic E2E test should inject fixed agent outputs so the pipeline proves:

1. baseline passes;
2. mutation is applied;
3. mutant survives;
4. candidate test is materialized;
5. candidate passes against original;
6. candidate produces TEST_FAIL against mutant;
7. final hardening result is verified;
8. the same batch is rerun with the candidate and the before/after score is recorded.

This is a test of PERJURY's pipeline, not of Gemini quality.

## 5. Live smoke requirements

### Modal
Record:
- sandbox count;
- per-sandbox duration;
- wall-clock duration;
- exit/outcome;
- cleanup confirmation.

Repeat the concurrency spike enough to detect obvious cleanup/reliability problems before the demo.

### Gemini
The smoke script must use `PERJURY_MODEL`, not a separate hard-coded model name. Validate structured output through PydanticAI.

### Full rehearsal
Record:
- commit SHA;
- configured model;
- run ID;
- mutation counts/statuses;
- selected survivor;
- verification evidence;
- total wall-clock duration.

## 6. Failure ownership

A deterministic failure blocks merging.

A live-service failure blocks the milestone only when the milestone explicitly requires that service. Classify whether the cause is:
- product logic;
- configuration/authentication;
- provider/network;
- quota/rate limit;
- unsupported target project.

Do not "fix" external failures by weakening deterministic correctness rules.
