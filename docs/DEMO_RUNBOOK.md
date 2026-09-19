# PERJURY — Demo Runbook

This is the operational script for preflight, rehearsal, and the live judge demo. Commands assume the
bootstrap virtualenv is active (`source .venv/bin/activate`); keep this file and the README in step.

## 1. Before the demo

Confirm:
- working tree is at the intended demo commit;
- deterministic quality gate from #22 is green;
- `.env` contains required credentials and is not committed;
- `PERJURY_MODEL` is the intended configured model;
- Modal authentication is valid;
- bundled refund fixture baseline is green.

Run the required preflight entrypoint (about 10 s once the Modal image is warm; the first run after a
Modal image change can take minutes while the image builds, which is exactly why you run it early):

```bash
python scripts/preflight.py        # exit code 0 only when every check passes
```

It checks, grouped by subsystem: **config** (Python 3.12, `GOOGLE_API_KEY`, `PERJURY_MODEL`,
`PERJURY_MUTATION_COUNT` 6–10, Modal credentials), **gemini** (a real structured PydanticAI response),
**modal** (auth/app + a canary Sandbox that starts, executes, terminates, and leaves 0 running),
**baseline** (refund baseline PASS locally and in Modal with the same manifest), **workspace** (mutation
applicator known-path self-check incl. isolation/cleanup) and **evidence** (`.perjury/runs/` writable,
atomic write, redaction self-test). A failed check blocks its dependents (shown as BLOCKED, not as extra
failures). A failed preflight blocks the claim that the live demo is ready.

Then rehearse the full judge path once (uses the real Gemini + Modal; saves the evidence bundle):

```bash
python scripts/closed_loop_smoke.py                 # fails if > 110 s or not VERIFIED
python scripts/closed_loop_smoke.py --mock-models   # same path, canned model outputs, live Modal
```

It prints the phase timings, `run_id`, the commit SHA (suffixed `-dirty` if tracked files differ from
HEAD — do not present a `-dirty` run as the frozen commit) and the evidence bundle path
(`.perjury/runs/<run_id>/evidence.json`, gitignored, sanitized).

Do not begin the live presentation with a failing preflight.

Validated implementation SHA: **`1429b6a47b0edd883807f74c631077405b2707b7`** (later commits are documentation-only).
Measured results, in three categories that must never be merged or presented as one batch:
- **Canonical reproducible rehearsal** (`--mock-models`, real Modal): 5 killed / 3 survived → 0.625,
  M01 VERIFIED, same-batch re-score 6 / 2 → 0.750, ~18–21 s.
- **Fully live Gemini + Modal, VERIFIED**, run `run-2e7df7037679` on the backend-identical `72985a0`:
  54.067 s, live same-batch re-score 0.000 → 0.125, evidence `sha256:0b29c589ff93…`, 0 leaked Sandboxes.
- **Fully live Gemini + Modal on `1429b6a`, safe rejections**: `run-b3705a55cea6` (47.655 s,
  `sha256:3c5b32c7df26…`) and `run-5d3839cf1daa` (51.958 s, `sha256:3b127030c415…`), both
  `rejected` / `candidate_failed_original`, 0 leaked Sandboxes. Gemini's test failed on the original code, so
  execution refused it; if this happens on stage, say so — that is the invariant working.

Launch for the judges: `python -m uvicorn perjury.api:app --port 8000` → http://127.0.0.1:8000/demo,
then press **Run PERJURY**. The page follows the run and ends on the proof and score panels.

## 2. Intended two-minute narrative

### 0:00–0:20 — Establish the gap
Show the refund-policy code and green baseline. Explain: green tests prove only what they cover.

### 0:20–0:50 — Adversarial mutation fan-out
Start one PERJURY run. Show 6–10 semantic mutations executing in isolated Modal Sandboxes and the killed/survived/invalid/timeout/infrastructure-error states.

### 0:50–1:15 — Inspect a real survivor
Select the premium-customer behavior survivor. Show the actual source diff and Gemini's behavioral explanation. Describe it as a potential test gap, not a confirmed production bug.

### 1:15–1:40 — Generate the missing test
Show the proposed pytest regression test and make clear that this is a model proposal, not proof.

### 1:40–2:00 — Prove it
Show:
- original + candidate => PASS;
- mutant + candidate => TEST_FAIL;
- deterministic verified result;
- same-batch before/after mutation score and evidence summary.

Close on the distinction: Gemini searches for the weakness; execution decides whether the hardening is valid.

## 3. Evidence to keep visible

Prefer showing:
- run ID;
- commit SHA where available;
- mutation ID;
- source diff;
- semantic execution outcomes;
- generated test;
- both verification outputs;
- wall-clock timing.

Avoid dumping noisy raw logs unless a judge asks.

## 4. Recovery paths

### Preflight failure, by subsystem
- **config** — `GOOGLE_API_KEY` empty/missing: put it in `.env` (never commit it). Bad `PERJURY_MODEL`: use
  `provider:model`, e.g. `google:gemini-3.8-flash`. `PERJURY_MUTATION_COUNT` outside 6–10: fix `.env`.
  No Modal credentials: `modal setup` (or export `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`). Wrong Python: use 3.12.x.
- **gemini** — structured response failed: check quota/billing and the key; run `python scripts/gemini_smoke.py`;
  confirm `PERJURY_MODEL`; if the provider is down do not claim a live model result.
- **modal** — auth/app failed: `modal token set` / `modal setup`, then retry. Canary failed or leaked a Sandbox:
  run `modal app list` / `modal container list`, stop stray containers of app `perjury`, rerun. First run after a
  dependency change rebuilds the image (slow once).
- **baseline** — local baseline red: STOP; restore the frozen commit / fixture. Modal baseline differs or fails:
  check the Modal error text (usually infra); rerun; do not weaken the check.
- **workspace** — applicator self-check failed: the source tree drifted from the canonical fixture (`examples/refund`);
  `git status`, restore the frozen commit.
- **evidence** — `.perjury/runs/` not writable: fix permissions/disk space (`df -h .`); the demo can still run but
  will not save a bundle, so fix it before rehearsing.

### During a run
- Run ended `failed`: open `/api/runs/<id>` (`error.code`) and `.perjury/runs/<id>/evidence.json`
  (`outcome`, `run.stages`) — they name the failing stage. Never present it as a success.
- Run ended `inconclusive`/`rejected`: that is a truthful result (e.g. `mutant_not_killed`,
  `rescore_inconsistent`); rerun once; do not edit results.
- Run over 110 s: run `preflight` again (Modal cold start/contention), then rehearse; check `stages` durations in
  the evidence bundle to see which phase slowed.


### Gemini smoke fails
- verify `GOOGLE_API_KEY`;
- verify `PERJURY_MODEL`;
- rerun the isolated Gemini smoke;
- if provider availability remains broken, do not claim a live model result.

### Modal smoke fails
- verify Modal authentication;
- run one Sandbox before the fan-out spike;
- inspect bootstrap/workspace failure classification;
- ensure failed Sandboxes are terminated.

### Baseline is red
Stop. Do not run mutation analysis against a failing baseline. Restore the frozen demo commit or fix the fixture, then repeat deterministic checks and preflight.

### Generated candidate is invalid
Display the rejection accurately (`rejected`, e.g. `candidate_failed_original` or `candidate_invalid`). The orchestrator does not retry a rejected candidate, so start a new run (live test generation is nondeterministic; the canonical `--mock-models` path is reproducible). Never convert an invalid candidate into a verified result.

### External service is unavailable during judging
A previously captured evidence bundle from the real pipeline may be shown **only if explicitly labelled as a recorded prior run**, including its commit SHA/run ID. Do not present it as live execution.

## 5. After any code change

If the demo commit changes after a successful rehearsal:
1. rerun deterministic checks;
2. rerun external preflight;
3. rerun the end-to-end rehearsal;
4. capture a new evidence bundle;
5. update the frozen SHA in the final submission evidence.

This prevents a last-minute cosmetic change from invalidating the rehearsed system.
