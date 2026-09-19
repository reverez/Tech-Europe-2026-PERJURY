# PERJURY — UI ↔ API contract (provisional, #18 / #17)

The demo UI (`perjury/ui/`, served at `/demo`) is zero-build vanilla JS with JSDoc types. All
field names live in `perjury/ui/contract.js`; when #17 lands, reconcile there only.

## Endpoints consumed
- `POST /api/runs` → `{ "run_id": str }` (409 typed error if a run is active)
- `GET /api/runs/{id}` → `RunSnapshot` (authoritative; used on refresh and on every terminal event)
- `GET /api/runs/{id}/events` → SSE, one JSON `RunEvent` per `message`

## Events
`run.started`, `baseline.completed`, `plan.completed`, `mutation.started`, `mutation.completed`,
`survivor.selected`, `analysis.completed`, `test.proposed`, `verification.completed`,
`rescore.completed`, `run.completed`, `run.failed`. Each carries `run_id`, monotonic `seq`,
`stage`, optional `mutation_id`, and `data`. Stale/duplicate `seq` values are ignored.

## Rules the UI enforces
- Score before/after come from the snapshot/event payload (#25); the browser never recomputes them.
- Panels are tagged FACT (executed), MODEL (Gemini proposal), DERIVED (metric).
- Terminal state is reconciled against `GET /api/runs/{id}`; a dropped stream also recovers from it.
- `#run=<id>` in the URL restores a run after refresh.
- All text is inserted as text nodes (no `innerHTML`).

## Mock adapter (development only)
`/demo?adapter=mock[&speed=N][&autostart=1]` replays a scripted event stream through the same reducer
and shows a permanent **SIMULATED** banner. It is not evidence and must never be used for judging.
Default is the live adapter.

## Tests
`node --test tests/ui/store.test.mjs` (also run from `pytest`) and `tests/test_ui.py` (serving).
