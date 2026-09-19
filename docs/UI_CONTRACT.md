# PERJURY — demo UI contract (#18)

The demo UI (`perjury/ui/`, served at `/demo`) is zero-build vanilla JS with JSDoc types: no npm, no CDN.
The **authoritative** API shapes are in [API_CONTRACT.md](./API_CONTRACT.md); the UI-side types live in
`perjury/ui/contract.js` and are kept in step with it.

## What the UI consumes (live mode, the default)
- `POST /api/runs` → 202 `{run_id}` (409 `detail.message` is shown when a run is active)
- `GET /api/runs/{id}` → authoritative snapshot (initial state, refresh, terminal reconciliation, recovery)
- `GET /api/runs/{id}/events[?after=seq]` → SSE, folded by `store.js reduce()`

## Rules the UI enforces
- **No score arithmetic in JS.** `score_before/after` and `comparison {delta, direction, status}` are copied
  from the backend. `Score.score` may be `null` → rendered **n/a**, never 0%. `deltaLabel()` only formats
  the backend delta; `comparison.status == "inconsistent"` is shown as a warning and no improvement is claimed.
- Outcomes are UPPERCASE (`baseline.outcome`, `verification.original/mutant`); mutation statuses lowercase.
- Stages: `baseline, context, planning, mutation_execution, survivor_analysis, test_generation, verification,
  rescoring` then a terminal (`verified/rejected/inconclusive/failed`); a stopped run highlights the stage it stopped at.
- `mutation.started` is not emitted: cards go `pending → killed/survived/…`.
- Provenance stays visible: **FACT** (executed), **MODEL** (Gemini proposal), **DERIVED** (metric).
- Recovery: a dropped stream re-reads the snapshot and resumes with `?after=<last_seq>` (bounded retries);
  every terminal event is reconciled against `GET /api/runs/{id}`; `#run=<id>` restores a run after refresh.
- All text is inserted as text nodes (no `innerHTML`); falsy conditional children are dropped, never stringified.

## Mock adapter (development only)
`/demo?adapter=mock[&speed=N][&autostart=1]` replays a scripted stream (the canonical refund result:
M01 selected, 5 killed / 3 survived = 62.5% → 6 killed / 2 survived = 75%) through the same reducer and
shows a permanent **SIMULATED** banner. It is not evidence and never used unless explicitly selected.

## Tests
- `node --test tests/ui/store.test.mjs` — reducer, formatting, mock fixture (run from `pytest`)
- `tests/test_ui.py` — serving/packaging assumptions; `tests/test_ui_integration.py` — real API output
  folded through the JS reducer (`tests/ui/api_contract.mjs`) for verified/failed/inconclusive/rejected/undefined-score runs
- `python scripts/browser_smoke.py` — real headless-Chrome run against the real API (mock-model orchestration)
