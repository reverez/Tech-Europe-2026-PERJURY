# PERJURY — Development Workflow

This is the operating contract for implementing the PERJURY backlog during the hackathon. It exists to keep human and coding-agent work consistent, minimize merge collisions, and prevent a polished demo from outrunning correctness.

## 1. Sources of truth

When two artifacts disagree, resolve them in this order:

1. **docs/SPEC.md** — product scope and non-negotiable correctness invariant.
2. **docs/ARCHITECTURE.md** — target component boundaries and execution semantics.
3. **docs/DECISIONS.md** — explicit architectural choices.
4. **docs/IMPLEMENTATION_PLAN.md** — milestone order and gates.
5. **GitHub issue** — concrete task scope and acceptance criteria.
6. **Current code** — implementation state, which may legitimately lag the target.

Do not silently change the invariant or MVP scope to make an implementation easier. If a real feasibility constraint requires a scope change, update the specification/decision log and affected issues in the same change.

## 2. Issue lifecycle

For each development issue:

1. Read the parent epic, dependencies, architecture, and relevant decisions.
2. Confirm every blocking issue is merged/green before depending on its contract.
3. Create a short-lived branch named `issue-<number>-<slug>`.
4. Add or update deterministic tests for the intended behavior/failure mode.
5. Implement the smallest complete slice satisfying the acceptance criteria.
6. Run the deterministic quality gate from #22.
7. Run any issue-specific live smoke only when the issue explicitly requires Modal/Gemini.
8. Update docs when commands, contracts, execution semantics, or user-visible behavior changed.
9. Merge only after the issue's acceptance criteria are demonstrably satisfied.
10. Mark the child issue complete, then update the parent checklist.

Recommended commit prefix: `feat(#N):`, `fix(#N):`, `test(#N):`, or `docs(#N):`.

## 3. Merge gate

Every merge must satisfy:

- deterministic tests pass;
- ruff passes;
- no unresolved TODO is required by the issue acceptance criteria;
- no credential or secret is committed;
- new public/serialized contracts have tests;
- failure paths are typed/classified rather than swallowed;
- docs do not claim behavior that is not implemented;
- external-service output is never substituted for deterministic proof.

Live Gemini/Modal tests are not required on every commit. They are required at the milestone gates that depend on those services.

## 4. Dependency discipline

An issue may start early for research or scaffolding, but it must not merge against an unstable upstream contract.

Critical path:

```text
#22 quality gate
  -> #4 workspace/baseline contract
  -> #8 execution taxonomy
  -> #5 mutation applicator
  -> #6 Modal materialization
  -> #7 fan-out

#9 context packer (can overlap late M0)
  -> #10 mutation planning
  -> #11 mutation execution
  -> #12 survivor analysis
  -> #13 generated-test materialization
  -> #14 two-sided verification
  -> #15 run state machine/orchestrator
  -> #16 deterministic E2E

#17 API/events
  -> #18 UI
#15 -> #19 evidence/observability
#17 + #18 + #19 -> #20 preflight/rehearsal

#20 + #22 -> #24 final validation/freeze
#24 -> #23 final documentation sign-off
```

## 5. Parallel-work lanes

Safe parallelism is encouraged only where contracts are stable.

| Lane | Primary issues | High-contention files |
| --- | --- | --- |
| Execution | #4–#8 | `contracts.py`, `modal_runner.py` |
| Agent pipeline | #9–#14 | `contracts.py`, `agent.py` |
| Orchestration/API | #15, #17, #19 | run/event contracts, `api.py` |
| Demo UI | #18 | frontend files/API schema |
| Validation/docs | #20, #23, #24 | scripts, README, docs |

Rules:
- only one active branch should change the same contract model unless changes are coordinated;
- rebase onto the dependency's merged commit before integrating;
- avoid broad refactors while another lane depends on the touched module;
- contract renames require a repository-wide search before merge.

## 6. Contract-change protocol

Changes to Pydantic models, enums, event shapes, or run-state semantics must include:

1. model change;
2. serialization/validation test;
3. consumer updates;
4. architecture/decision update if semantics changed;
5. API/example payload update when externally visible.

No consumer should infer meaning from raw process exit codes after #8.

## 7. External-service protocol

Gemini and Modal are nondeterministic/external dependencies.

- deterministic CI must not require credentials;
- model outputs in deterministic tests are fixtures/mocks validated through the same Pydantic contracts;
- live smoke tests must clearly identify themselves;
- failures caused by credentials, quotas, provider availability, or network are classified separately from product logic failures;
- a recorded real demo run may be used as a disclosed backup artifact, but never presented as a live execution.

## 8. Documentation update matrix

| Change | Required docs |
| --- | --- |
| correctness/scope invariant | SPEC + ARCHITECTURE + DECISIONS |
| execution status/contract | ARCHITECTURE + TEST_STRATEGY + issue |
| dependency/runtime support | SPEC/ARCHITECTURE + README setup |
| API/event shape | ARCHITECTURE + demo runbook |
| command/setup change | README + DEMO_RUNBOOK |
| milestone/dependency change | IMPLEMENTATION_PLAN + parent/child issues |
| final demo behavior | README + DEMO_RUNBOOK + evidence |

## 9. Stop-the-line conditions

Do not proceed to downstream demo polish when any of these is true:

- baseline cannot run reliably;
- semantic execution taxonomy is ambiguous;
- generated tests can be verified by non-test failures;
- Sandboxes leak or time out unpredictably;
- the deterministic E2E test is red;
- API/UI displays data that cannot be traced to real run state;
- docs and actual commands disagree.

Fix the upstream defect first, then resume downstream work.

## 10. Definition of done

A child issue is done only when its implementation, deterministic tests, documented assumptions, and acceptance criteria agree. A parent epic is done only when all children are done **and** its milestone exit gate has been exercised end-to-end.
