# PERJURY — Frozen Hackathon Specification

**Revision:** 1.1 — correctness and execution semantics clarified after implementation audit.

"Frozen" means the product thesis, correctness boundary, and MVP scope should not drift casually during the hackathon. Corrections that remove ambiguity must be reflected in the architecture, decisions, tests, and issues.

## Thesis

PERJURY is an autonomous adversarial test-hardening agent. It applies semantic mutations to a supported Python/pytest workspace, executes those mutations in isolated Modal Sandboxes, identifies surviving mutants, asks Gemini for targeted regression tests, and accepts a generated test only after deterministic two-sided verification.

## Non-negotiable invariant

A generated regression test is **verified** only when:

- original program + generated test → **PASS**
- surviving mutant + generated test → **TEST_FAIL**

`TEST_FAIL` specifically means pytest executed normally and reported a failing test.

These outcomes do **not** qualify as proof:
- collection error;
- no tests collected;
- command/usage error;
- timeout;
- dependency/bootstrap failure;
- Sandbox/provider failure;
- arbitrary non-zero process exit.

The LLM never decides the final verdict.

## MVP target contract

The hackathon MVP supports:

1. Python 3.12+.
2. pytest.
3. One immutable repository/workspace snapshot per run.
4. A known structured install command when installation is required.
5. A known structured pytest command.
6. Approximately 6–10 semantic mutation trials.
7. The bundled refund fixture as the canonical first end-to-end demonstration.

The MVP does not attempt to infer arbitrary build systems or dependency strategies.

## MVP acceptance

1. Deterministic local/CI checks are green without Gemini or Modal credentials.
2. Baseline suite is demonstrably PASS through the production execution contract.
3. Gemini/PydanticAI returns a validated mutation batch.
4. At least 6 valid mutation trials execute through Modal.
5. Execution outcomes distinguish PASS, TEST_FAIL, INVALID, TIMEOUT, and INFRA_ERROR.
6. Mutation presentation distinguishes killed/survived without hiding invalid/error trials.
7. At least one meaningful survivor can be selected.
8. Gemini generates a targeted pytest candidate for that survivor.
9. Candidate materialization cannot alter the original workspace.
10. Two-sided execution either verifies, rejects, or marks the candidate inconclusive deterministically.
11. Only original PASS + mutant TEST_FAIL can produce VERIFIED.
12. The complete real-service loop is visible in a demo within two minutes after successful preflight.
13. Evidence can identify the run and the code revision used for the final rehearsal.

## Safety and correctness language

A surviving mutant is a **potential test gap**, not automatically a production bug. Equivalent mutants may exist.

Model-generated mutation descriptions, analyses, and tests are proposals. Pytest/Modal execution results are evidence. The UI and documentation must keep those categories visually and semantically distinct.

## Explicit non-goals for the hackathon

- multi-language mutation;
- arbitrary dependency/build-system discovery;
- production multi-tenant isolation;
- GitHub PR automation;
- formal proof of mutant equivalence;
- databases or arbitrary external target services;
- general multi-agent orchestration.

Any expansion beyond this list must not enter the critical path before the MVP gates are green.
