# PERJURY — Recursive Audit Checklist

Run this audit at each milestone boundary and after any substantial shared-contract change.

The purpose is not to chase cosmetic perfection. It is to catch defects that can make downstream implementation ambiguous, nondeterministic, unsafe, unreproducible, or misleading in the demo.

## A. Establish the exact revision

- [ ] Record the current `main` commit SHA.
- [ ] Confirm whether another writer/agent is actively advancing `main`.
- [ ] If multiple writers are active, switch to issue/audit branches per [DEVELOPMENT_WORKFLOW.md](./DEVELOPMENT_WORKFLOW.md).
- [ ] Treat only CI for the exact audited SHA as authoritative.
- [ ] If `main` advances during the audit, restart affected checks against the new head.

## B. Specification consistency

Compare:
- [SPEC.md](./SPEC.md)
- [ARCHITECTURE.md](./ARCHITECTURE.md)
- [DECISIONS.md](./DECISIONS.md)
- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)

Check:

- [ ] The non-negotiable verification invariant is identical everywhere.
- [ ] Supported runtime/language/test framework scope matches.
- [ ] Non-goals have not silently entered the critical path.
- [ ] Execution outcome names have one meaning everywhere.
- [ ] Mutation status is not confused with generic execution outcome.
- [ ] Claims about partner technologies match actual load-bearing code paths.

## C. Dependency graph audit

For every open implementation issue:

- [ ] Parent epic is correct.
- [ ] Dependencies refer to real issue numbers.
- [ ] Dependency graph is acyclic.
- [ ] A child never requires implementation that only exists in its downstream dependent.
- [ ] Shared contracts are defined before provider/UI consumers.
- [ ] Parent checklists include every child issue.
- [ ] Issue titles/numbers match docs.
- [ ] The next executable issue has all blockers complete.

Useful rule: interface/contract → deterministic tests → provider implementation → integration → API/evidence → UI → live rehearsal.

## D. Contract and evidence audit

For every public Pydantic model/serialized object:

- [ ] Contradictory states are rejected, not merely discouraged.
- [ ] Identity fields agree across nested evidence.
- [ ] Raw provider/process values are not treated as business semantics.
- [ ] Derived values can be recomputed from retained evidence.
- [ ] Result objects cannot claim success when evidence says otherwise.
- [ ] Model-generated content is labelled as proposal, not executed fact.
- [ ] Every final demo claim can be traced to evidence.

For verification specifically:

- [ ] Only original PASS + mutant TEST_FAIL can verify.
- [ ] INVALID/TIMEOUT/INFRA_ERROR never verify.
- [ ] Original TEST_FAIL rejects the generated candidate.
- [ ] Other non-PASS original outcomes are inconclusive/failure as specified.
- [ ] Original/mutant worlds share baseline snapshot and candidate identity.

## E. External-boundary audit

### Modal

- [ ] No Modal lookup/client operation occurs at module import.
- [ ] Commands remain structured argv; no shell interpolation.
- [ ] Only pytest commands enter the pytest semantic mapper.
- [ ] Workspace paths are validated before any provider call.
- [ ] Workspace root/path cannot escape the intended sandbox root.
- [ ] Snapshot upload excludes secrets/runtime artifacts.
- [ ] Outbound network is blocked by default.
- [ ] Timeout is bounded.
- [ ] Cleanup occurs on every terminal path.
- [ ] Provider/bootstrap failures remain distinct from pytest failures.
- [ ] Runtime dependency versions match the frozen hackathon environment.

### Gemini/PydanticAI

- [ ] Importing deterministic code does not require credentials/network.
- [ ] One configured model source is used.
- [ ] Repository source/comments are untrusted prompt data.
- [ ] Structured outputs pass Pydantic validation before execution.
- [ ] Deterministic tests can inject/mock the model boundary.
- [ ] Retries/replenishment are explicitly bounded.

## F. Workspace and filesystem audit

- [ ] Absolute paths rejected.
- [ ] `..` traversal rejected.
- [ ] Symlink escape rejected where host filesystem paths are involved.
- [ ] Mutation targets are implementation-only.
- [ ] Tests/context files cannot be mutated.
- [ ] Generated candidate tests use create-new semantics.
- [ ] Existing user files are not overwritten/appended.
- [ ] Snapshot file-count and byte limits are enforced (`max_snapshot_files`, `max_snapshot_bytes`).
- [ ] Candidate/snapshot identity is recorded in evidence.

## G. Resource and output audit

- [ ] Per-command timeout is bounded.
- [ ] Mutation concurrency is bounded.
- [ ] Agent retries are bounded.
- [ ] stdout/stderr capture is bounded (`max_output_bytes`).
- [ ] Truncation is explicit in evidence.
- [ ] Invalid/timeouts/infra failures are excluded from mutation-score denominator.
- [ ] Exact before/after score uses the same mutation batch and actual reruns.

## H. Deterministic quality gate

Run:

```bash
bash scripts/check.sh
```

Confirm:

- [ ] `pip check`
- [ ] Ruff
- [ ] Python compilation
- [ ] offline API/import smoke
- [ ] unit/integration pytest

CI:

- [ ] Exact audited SHA has a completed green `deterministic-ci` run.
- [ ] A stale/cancelled older run is not mistaken for current-head evidence.
- [ ] No default CI step requires Gemini/Modal credentials.

A pending/queued workflow is **not** evidence of success.

## I. Live-service gate

Only at milestones that require it:

### Modal
- [ ] Authentication/preflight passes.
- [ ] One canary workspace passes.
- [ ] Required fan-out passes.
- [ ] Cleanup is observed.
- [ ] No secrets are forwarded.
- [ ] Network policy matches WorkspaceSpec.

### Gemini
- [ ] Configured `PERJURY_MODEL` smoke passes.
- [ ] Structured response validates.
- [ ] Provider failure is distinguished from product failure.

### Full rehearsal
- [ ] Real end-to-end run completes.
- [ ] Run ID and commit SHA captured.
- [ ] Evidence bundle persisted.
- [ ] Demo wall time meets target.

## J. API/UI/evidence audit

Once M2 exists:

- [ ] API drives the same #15 orchestrator as tests.
- [ ] One active-run policy is enforced.
- [ ] SSE event ordering agrees with authoritative run snapshot.
- [ ] UI never recomputes authoritative score/verdict.
- [ ] UI distinguishes model proposal / executed fact / derived metric.
- [ ] No canned success JSON is presented as execution.
- [ ] Persisted evidence is sanitized and atomically written.
- [ ] Recorded backup demo is visibly labelled recorded, not live.

## K. Documentation drift audit

Search for:
- [ ] obsolete issue numbers/titles;
- [ ] stale commands;
- [ ] stale dependency versions;
- [ ] wording like “non-zero means fail/verified”;
- [ ] claims of arbitrary repository/dependency support;
- [ ] outdated next-step instructions;
- [ ] docs that call model output evidence;
- [ ] old UI/API route names.

Any command in README/DEMO_RUNBOOK must match real executable behavior.

## L. Closeout rule

A child issue may close only when:
1. every acceptance criterion is satisfied;
2. deterministic tests cover the new invariant/failure modes;
3. exact-head deterministic CI is green;
4. affected docs are synchronized;
5. required live-service evidence exists, when applicable.

A parent epic closes only when every child is complete **and** the integrated milestone exit gate has been exercised.

If any check fails, fix the earliest upstream cause, rerun this checklist from the affected section, and do not paper over the failure downstream.
