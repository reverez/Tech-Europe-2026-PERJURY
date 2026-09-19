# PERJURY — Implementation Plan

**Hackathon date:** 19 September 2026  
**Scope:** Python + pytest, Gemini/PydanticAI, Modal Sandboxes, FastAPI, one reliable refund-policy demo.

This plan turns the existing scaffold into four implementation milestones. The large original issues (#1–#3) remain parent epics; the new issues are executable development units.

## Status at audit

Already present:
- typed mutation/analysis/test/verification contracts;
- Gemini/PydanticAI agent definitions;
- minimal Modal Sandbox primitive;
- deterministic verification function;
- FastAPI health/root endpoints;
- refund demo fixture;
- basic contract and verification tests;
- Modal and Gemini smoke scripts.

Not yet connected:
- real repository/workspace materialization;
- deterministic mutation application;
- bounded production fan-out;
- safe execution-status semantics;
- repository context packing;
- complete agent orchestration;
- generated-test materialization;
- real two-world verification execution;
- run API/event stream;
- demo UI;
- observability/evidence bundle;
- CI/reproducibility hardening.

## Milestone M0 — Execution foundation

**Parent:** #1 — P0: prove Modal sandbox fan-out

Issues:
- #4 — baseline runner and workspace contract
- #5 — deterministic mutation applicator
- #6 — runnable project workspaces in Modal
- #7 — bounded concurrent fan-out
- #8 — execution taxonomy and verification semantics

### Exit gate

M0 is complete only when the refund fixture runs through the same Modal execution boundary intended for mutants, 10 jobs can fan out and clean up reliably, and invalid/infra executions cannot masquerade as mutation kills or verified tests.

### Critical note

#8 is a correctness blocker. The current scaffold checks `mutant_exit_code != 0`; that is too weak because infrastructure or collection failures are also non-zero.

---

## Milestone M1 — Closed autonomous hardening loop

**Parent:** #2 — P1: close mutation → hardening loop

Issues:
- #9 — source/test context packer
- #10 — mutation planning, validation, deduplication
- #11 — mutation execution/classification
- #12 — survivor analysis/selection
- #13 — safe generated-test materialization
- #14 — deterministic original-vs-mutant verification
- #15 — complete orchestration service
- #16 — deterministic end-to-end integration test

### Exit gate

One callable core pipeline must:
1. prove a green baseline;
2. obtain a validated mutation batch;
3. execute at least 6 mutations through Modal;
4. retain killed/survived/invalid/timeout evidence;
5. select a real survivor;
6. obtain a targeted test proposal;
7. execute the test against original and mutant;
8. return a typed verified/rejected/inconclusive hardening result.

The deterministic CI path should mock model outputs; a separate smoke path can exercise live Gemini/Modal.

---

## Milestone M2 — Live demo and observability

**Parent:** #3 — P2: two-minute live demo surface

Issues:
- #17 — typed run API and event stream
- #18 — single-screen demo surface
- #19 — structured observability/evidence bundle
- #20 — preflight and sub-two-minute rehearsal

### Exit gate

A judge can click one primary CTA and see the real pipeline progress from green baseline through mutation fan-out to a selected survivor, generated regression test, and two-sided proof in under two minutes.

The UI must never use canned success JSON in place of execution.

---

## Milestone M3 — Submission hardening

**Parent:** #21 — P3: submission hardening and judging evidence

Issues:
- #22 — CI quality gates
- #23 — architecture/setup/demo/judging documentation

### Exit gate

Fresh-clone setup works, deterministic quality gates are green, the demo runbook is reproducible, evidence can be inspected, and the README makes the partner technologies and correctness invariant immediately understandable.

---

## Recommended implementation order

```text
#4  baseline/workspace
  -> #5 mutation applicator
  -> #6 Modal workspace execution
  -> #7 bounded fan-out
  -> #8 execution semantics

#9 context packer
  -> #10 mutation planner
  -> #11 mutation execution
  -> #12 survivor analysis
  -> #13 test materialization
  -> #14 two-sided execution
  -> #15 orchestrator
  -> #16 deterministic E2E

#17 API/events
  -> #18 UI
  -> #19 evidence/observability
  -> #20 preflight/rehearsal

#22 CI
  -> #23 final docs/evidence
```

Where useful, #9 can proceed in parallel with #6–#8, and the basic UI shell in #18 can be prototyped after the event schema in #17 is fixed. Do not let UI work delay the M0/M1 correctness gates.

## Definition of done for any issue

An issue is not complete until:
- implementation exists;
- relevant contracts are updated;
- tests cover the new invariant/failure path;
- errors are typed or explicitly classified;
- README/docs are updated when a user-visible command or architecture assumption changes;
- no result is presented as executed evidence unless it came from execution.

## Scope controls

Do not expand the hackathon build into:
- multi-language mutation;
- arbitrary package-manager support;
- GitHub PR automation;
- formal equivalent-mutant proofs;
- persistent multi-tenant infrastructure;
- general multi-agent orchestration.

Those are post-hackathon directions, not current blockers.
