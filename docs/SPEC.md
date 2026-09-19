# PERJURY — Frozen Hackathon Specification

## Thesis

PERJURY is an autonomous adversarial test-hardening agent. It sabotages a Python codebase with semantic mutations, executes those mutations against the real pytest suite in isolated Modal Sandboxes, identifies surviving mutants, generates targeted tests with Gemini, and accepts those tests only after deterministic two-sided verification.

## Non-negotiable invariant

A generated regression test is **verified** only when:

- original program + generated test → **PASS**
- surviving mutant + generated test → **FAIL**

The LLM never decides this verdict.

## MVP acceptance

1. Baseline suite is demonstrably green.
2. Gemini/PydanticAI returns a validated mutation batch.
3. At least 6 mutations execute through Modal.
4. Results are typed as killed, survived, invalid, or timeout.
5. At least one meaningful survivor can be selected.
6. Gemini generates a targeted pytest test for that survivor.
7. Two-sided execution either verifies or rejects the test deterministically.
8. The complete loop is visible in a demo within two minutes.

## Language scope

Python + pytest only for the hackathon build.

## Safety/correctness language

A surviving mutant is a **potential test gap**, not automatically a production bug. Equivalent mutants may exist.
