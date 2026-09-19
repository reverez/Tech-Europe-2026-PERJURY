# PERJURY — Demo Runbook

This is the operational script for preflight, rehearsal, and the live judge demo. Commands may evolve as #20 is implemented; when they do, update this file and README together.

## 1. Before the demo

Confirm:
- working tree is at the intended demo commit;
- deterministic quality gate from #22 is green;
- `.env` contains required credentials and is not committed;
- `PERJURY_MODEL` is the intended configured model;
- Modal authentication is valid;
- bundled refund fixture baseline is green.

Once #20 is implemented, run the required preflight entrypoint:

```bash
python scripts/preflight.py
```

Until that command exists, the constituent smoke paths are:

```bash
pytest -q
python scripts/gemini_smoke.py
python scripts/modal_spike.py
```

Do not begin the live presentation with a failing preflight.

## 2. Intended two-minute narrative

### 0:00–0:20 — Establish the gap
Show the refund-policy code and green baseline. Explain: green tests prove only what they cover.

### 0:20–0:50 — Adversarial mutation fan-out
Start one PERJURY run. Show 6–10 semantic mutations executing in isolated Modal Sandboxes and the killed/survived/invalid/timeout states.

### 0:50–1:15 — Inspect a real survivor
Select the premium-customer behavior survivor. Show the actual source diff and Gemini's behavioral explanation. Describe it as a potential test gap, not a confirmed production bug.

### 1:15–1:40 — Generate the missing test
Show the proposed pytest regression test and make clear that this is a model proposal, not proof.

### 1:40–2:00 — Prove it
Show:
- original + candidate => PASS;
- mutant + candidate => TEST_FAIL;
- deterministic verified result;
- mutation score/evidence summary.

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
Display the rejection accurately. If the orchestration implements a bounded retry policy, retry; otherwise choose another survivor/run. Never convert an invalid candidate into a verified result.

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
