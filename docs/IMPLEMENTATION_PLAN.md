# PERJURY — Implementation Plan

**Hackathon date:** 19 September 2026  
**Scope:** Python + pytest, Gemini/PydanticAI, Modal Sandboxes, FastAPI, one reliable refund-policy demo.

This plan is the dependency map for the implementation issues. Parent epics describe milestone outcomes; child issues are mergeable units.

## Current scaffold

Present today:
- mutation/analysis/test/verification contracts;
- Gemini/PydanticAI agent definitions;
- minimal Modal Sandbox primitive;
- deterministic verifier scaffold;
- FastAPI health/root endpoints;
- refund demo fixture;
- basic tests;
- deterministic bootstrap/check script and GitHub CI;
- Gemini and Modal smoke scripts;
- architecture/workflow/test/risk/demo documentation.

Important known gaps remain intentionally tracked as issues. The code should not be described as the completed product until the milestone gates are green.

## M0 — Deterministic execution foundation

**Parent:** #1

Issues, in required order:

1. **#22** — deterministic local + CI quality gate
2. **#4** — WorkspaceSpec and baseline runner
3. **#8** — semantic execution taxonomy and verdict semantics
4. **#5** — deterministic safe mutation applicator
5. **#6** — real workspace materialization in Modal
6. **#7** — bounded concurrent mutation fan-out

#4 and #8 should be coordinated because the baseline result consumes the semantic execution contract.

### M0 exit gate

M0 is complete only when:
- deterministic checks are green without external credentials;
- refund baseline runs through the production execution contract;
- Modal executes the real copied workspace;
- 10 bounded trials can complete with cleanup;
- every result has a semantic execution outcome;
- collection/bootstrap/provider errors cannot masquerade as kills or verification evidence.

Do not make UI implementation the critical path before this gate.

---

## M1 — Closed autonomous hardening loop

**Parent:** #2

Issues:
1. **#9** — source/test context packer
2. **#10** — mutation planning, validation, and deduplication
3. **#11** — apply/execute mutations and compute valid score
4. **#12** — survivor selection and analysis
5. **#13** — safe generated-test materialization
6. **#14** — deterministic original-vs-mutant execution
7. **#25** — re-score the same mutation batch after verified hardening
8. **#15** — typed run state machine + complete orchestrator
9. **#16** — deterministic end-to-end refund integration test

#9 may begin late in M0 after WorkspaceSpec stabilizes. Downstream M1 work must consume the merged M0 execution semantics rather than reimplementing them.

### M1 exit gate

One callable core pipeline must:
1. prove a green baseline;
2. obtain a validated mutation batch;
3. execute at least 6 valid mutation trials through Modal in the live path;
4. preserve killed/survived plus invalid/error evidence;
5. select a real meaningful survivor;
6. obtain a targeted test proposal;
7. materialize it safely;
8. execute it against original and mutant;
9. return a typed verified/rejected/inconclusive result;
10. re-run the same validated mutation batch after a verified candidate and compute the post-hardening score;
11. pass a deterministic mocked-agent E2E test in CI.

---

## M2 — Live demo and observability

**Parent:** #3

Issues:
- **#17** — same-origin typed run API + SSE event stream, in-memory one-run registry
- **#18** — zero-build single-screen demo surface
- **#19** — structured observability + gitignored local evidence bundle
- **#20** — external preflight + sub-two-minute rehearsal

Dependencies:
- #17 requires the run/event contracts from #15.
- #19 requires #15 and can proceed in parallel with #17.
- #18 requires stable API/event shapes from #17.
- #20 requires #17, #18, and #19.

### M2 exit gate

A judge can start one real run and observe the pipeline from baseline to deterministic proof in under two minutes after successful preflight. The UI clearly separates model proposals from executed facts. No fabricated success data is used.

---

## M3 — Submission validation and evidence freeze

**Parent:** #21

Issues:
- **#24** — fresh-clone/live-service/full-demo validation and frozen commit evidence
- **#23** — final documentation sign-off against that frozen revision

### M3 exit gate

- fresh clone reaches green deterministic checks;
- configured Gemini model smoke passes;
- Modal preflight passes;
- real full demo completes;
- evidence records run ID and commit SHA;
- the validated demo commit is frozen;
- README/runbook/architecture match that exact implementation;
- any later code change forces re-preflight and re-rehearsal.

---

## Critical dependency graph

```text
#22
  ↓
#4 ─────→ #9
 ↓         ↓
#8       #10
 ↓         ↓
#5       #11
 ↓         ↓
#6       #12
 ↓         ↓
#7       #13
           ↓
          #14
           ↓
          #25
           ↓
          #15 ─────→ #19
           ↓           │
          #16          │
           │           │
           └──→ #17 ───┘
                 ↓
                #18
                 │
                 └──→ #20
                       ↓
                      #24
                       ↓
                      #23
```

#11 depends on both #7 and #10 even though the ASCII graph emphasizes the two lanes separately.

## Parallel implementation lanes

| Lane | Work | Merge constraint |
| --- | --- | --- |
| Execution | #4, #8, #5, #6, #7 | serialize shared execution contracts |
| Agent pipeline | #9–#14, #25 | consume merged WorkspaceSpec/execution semantics; re-score the exact same batch |
| Orchestration/API | #15, #17, #19 | #15 fixes run/event contract first |
| UI | #18 | use real API/event schema only |
| Validation/docs | #20, #24, #23 | validate exact commit being documented |

See [DEVELOPMENT_WORKFLOW.md](./DEVELOPMENT_WORKFLOW.md) for branch/merge rules.

## Definition of done for a child issue

An issue is done only when:
- implementation exists;
- relevant deterministic tests exist and pass;
- public/serialized contract changes have validation tests;
- errors are typed/classified;
- no external side effect is introduced at import time;
- workspace/file operations cannot escape allowlists through traversal/symlinks;
- secret/runtime paths are not blindly uploaded or serialized;
- subprocess output is bounded where persisted/streamed;
- documentation is updated when commands/contracts/scope changed;
- acceptance criteria are checked against actual behavior;
- no result is presented as executed evidence unless it came from execution.

## Definition of done for a parent epic

Checking every child issue is necessary but not sufficient. The parent exit gate must be exercised as an integrated path before the parent closes.

## Scope controls

Do not put these on the hackathon critical path:
- multi-language mutation;
- arbitrary dependency/build-system inference;
- GitHub PR automation;
- formal equivalent-mutant proofs;
- persistent multi-tenant infrastructure;
- general multi-agent orchestration.

## Recursive audit rule

After each parent milestone:
1. compare code to SPEC/ARCHITECTURE/DECISIONS;
2. compare merged behavior to every child acceptance criterion;
3. run deterministic checks;
4. run required milestone smoke/rehearsal;
5. search docs/issues for stale dependencies or claims;
6. fix upstream flaws before opening the next milestone gate.

This is the same audit loop used to produce the current roadmap.
