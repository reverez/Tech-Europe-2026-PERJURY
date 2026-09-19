# PERJURY — Hackathon Risk Register

| Risk | Trigger / signal | Mitigation | Fallback | Tracking |
| --- | --- | --- | --- | --- |
| Modal workspace does not match local execution | imports/setup differ | explicit WorkspaceSpec + parity test | narrow demo to bundled fixture | #6 |
| Sandbox fan-out leaks or stalls | jobs remain alive / high tail latency | bounded concurrency, timeouts, finally cleanup | reduce mutation count while preserving real execution | #7 |
| Pytest error misclassified as kill | non-1 exit accepted as TEST_FAIL | semantic taxonomy + verdict matrix | stop demo; never weaken verifier | #8/#14 |
| Gemini returns invalid/no-op mutations | validation/applicator rejection rate high | Pydantic validation + preflight + dedupe | retry within strict bounded policy or use another valid proposal | #10 |
| Equivalent mutant selected | test intent cannot distinguish behavior | possible-equivalence flag + survivor selection policy | select another survivor; report inconclusive | #12 |
| Generated test is invalid | syntax/collection failure | preflight in isolated workspace | reject candidate; optionally request another within bounded policy | #13 |
| Gemini model/config changes | smoke model unavailable | single `PERJURY_MODEL` source | switch configured supported model and rerun preflight | #20/#24 |
| External quota/network failure | provider errors/timeouts | preflight early; classify provider failure | show previously captured **real** run evidence, clearly labelled recorded, not live | #20/#24 |
| Re-score contradicts selected verification | selected mutant does not reproduce as killed | same-batch replay + inconsistency state | do not claim score improvement; surface inconsistency | #25 |
| Demo exceeds two minutes | rehearsal wall time too high | bounded 6–10 mutants, one survivor, minimal UI | reduce live mutant count within spec floor or optimize setup reuse | #20 |
| Scope creep | new language/GitHub automation/etc. appears on critical path | enforce SPEC + decisions | defer to post-hackathon backlog | parent epics |
| Parallel agent merge collision | same contract file edited concurrently | lane ownership + short branches + rebase | serialize contract changes | DEVELOPMENT_WORKFLOW |
| Documentation drift | commands/issue numbers no longer match code | doc update matrix + final audit | block #23 sign-off | #23 |
| Secret copied into Sandbox | .env/local runtime files appear in upload manifest | sanitized WorkspaceSpec manifest + denylist + tests | abort run; fix manifest before execution | #4/#6 |
| Secret leakage | keys appear in logs/evidence | env-only credentials + bounded output + redaction | revoke key, purge artifact, rerun | #19 |
| Demo code changes after successful rehearsal | commit SHA differs | evidence freeze records SHA | rerun preflight/rehearsal before presenting | #24 |

## Risk policy

Correctness risks outrank presentation risks. In particular, never make the demo appear successful by weakening execution classification, bypassing two-sided verification, or substituting fabricated output.

A recorded run is acceptable only as an explicitly labelled backup artifact that was produced by the real pipeline and carries a commit SHA/run ID.
