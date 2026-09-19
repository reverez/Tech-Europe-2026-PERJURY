## Issue

Closes #

## Dependency check

- [ ] Blocking issues in the task's `## Dependencies` section are merged/green.
- [ ] This branch was rebased/updated after the latest blocking contract change.

## What changed

<!-- Keep this implementation-scoped. Call out any contract, execution, API, or documentation change. -->

## Deterministic gate

- [ ] `bash scripts/check.sh` passes.
- [ ] New/changed serialized contracts have validation/round-trip tests.
- [ ] New failure modes have deterministic tests.
- [ ] No Gemini or Modal credential is required by default CI.

## Safety / correctness

- [ ] No secret or full process environment is committed/persisted.
- [ ] Paths are resolved inside the intended workspace; traversal/symlink escape is covered where relevant.
- [ ] External-service failures are not reclassified as deterministic success.
- [ ] Model output is treated as proposal/data, not correctness evidence.
- [ ] Verification semantics remain original PASS + mutant TEST_FAIL only.

## Live smoke

- [ ] Not required for this issue, **or**
- [ ] Gemini smoke executed and result noted below.
- [ ] Modal smoke/preflight executed and result noted below.

<!-- Live smoke is only mandatory when the issue/milestone explicitly depends on that provider. -->

## Documentation

- [ ] README/docs unchanged because behavior/contracts did not change, **or**
- [ ] affected docs/decisions/runbook were updated in the same change.
- [ ] issue acceptance criteria still match the implementation.

## Evidence / notes

<!-- Add run ID, commit SHA, relevant test output, or known limitation when useful. -->
