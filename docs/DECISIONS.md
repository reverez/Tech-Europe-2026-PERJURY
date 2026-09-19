# PERJURY — Architecture Decision Log

This file records decisions that downstream issues should treat as settled unless a new entry explicitly supersedes them.

| ID | Decision | Status |
| --- | --- | --- |
| ADR-001 | LLMs propose; execution proves. Final verification is deterministic. | Accepted |
| ADR-002 | Execution uses semantic outcomes PASS, TEST_FAIL, INVALID, TIMEOUT, INFRA_ERROR. | Accepted |
| ADR-003 | Commands are structured argv values, not interpolated shell strings. | Accepted |
| ADR-004 | Every mutation/test trial operates on an isolated immutable workspace snapshot. | Accepted |
| ADR-005 | Hackathon MVP does not promise arbitrary dependency resolution. Workspace config supplies a known install/test command. | Accepted |
| ADR-006 | SSE is the default run-event transport for the single-screen demo unless implementation evidence shows it is unsuitable. | Accepted |
| ADR-007 | The UI projects real run state; canned/fabricated success data is forbidden. | Accepted |
| ADR-008 | Default CI is deterministic and credential-free; Gemini/Modal are explicit smoke/rehearsal layers. | Accepted |
| ADR-009 | The Gemini model is configured through `PERJURY_MODEL`; scripts must not silently hard-code a different model. | Accepted |
| ADR-010 | The refund-policy fixture is the canonical first end-to-end demo and regression fixture. | Accepted |
| ADR-011 | Mutation score counts valid killed/survived mutants only; invalid/timeouts/errors are reported separately. | Accepted |
| ADR-012 | A surviving mutant is a potential test gap, not automatically a production bug. | Accepted |

## ADR-001 — Deterministic proof boundary

Gemini is useful for semantic proposal and explanation, but the acceptance decision must be independently executable. A candidate regression test is verified only when it passes on the original program and causes a normal pytest test failure on the mutant.

## ADR-002 — Semantic execution outcomes

Raw exit codes are evidence, not business semantics. The executor maps process results into the shared semantic taxonomy. Verification consumes the taxonomy.

## ADR-005 — Dependency support

The MVP targets a known Python 3.12/pytest workspace with an explicit setup contract. PERJURY should fail clearly on an unsupported target rather than guessing how to build arbitrary repositories.

## ADR-006 — Event transport

The demo is primarily server-to-client progress. SSE keeps the protocol simpler than a bidirectional socket and is sufficient unless the implementation discovers a concrete blocker.

## ADR-008 — CI boundary

Provider credentials, quota, latency, and outages should not make every development commit nondeterministic. Live services are tested at explicit preflight/rehearsal gates, while core logic is continuously covered by deterministic fixtures.

## Changing a decision

A replacement decision must:
1. name the ADR it supersedes;
2. state why the existing decision is no longer viable;
3. update architecture/test documentation and affected issues;
4. preserve the non-negotiable specification invariant.
