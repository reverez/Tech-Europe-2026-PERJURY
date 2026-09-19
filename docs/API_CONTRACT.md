# PERJURY — Run API contract (#17)

Same-origin FastAPI + SSE over the existing `run_perjury` orchestration (`perjury/orchestrator.py`).
Nothing here re-implements orchestration; `perjury/runs.py` starts/observes it and `perjury/api.py`
serves it. Models are typed (see `/openapi.json`).

## Endpoints
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | process health only |
| POST | `/api/runs` | **202** `RunCreated {run_id, stage, snapshot_url, events_url}`; **409** if a run is active |
| GET | `/api/runs/{run_id}` | authoritative `RunSnapshot`; **404** typed for unknown ids |
| GET | `/api/runs/{run_id}/events` | SSE, one typed `RunEvent` JSON per default `message`, `id:` = `seq` |

Errors are `{"detail": {"code", "message", "active_run_id?"}}` with `code` in
`run_active | run_not_found | invalid_request` (the UI already reads `detail.message`).

Policy: in-memory registry, **one active run at a time**, no database/WebSocket/CORS, up to 50 runs
retained. Live Gemini/Modal boundaries are built when a run starts, never at import.

## SSE, reconnect and terminal agreement
- Events are ordered `seq` 1..N with `run_id`, `stage`, `correlation_id`, optional `mutation_id`.
- Resume with the standard `Last-Event-ID` header or `?after=<seq>`; a finished run replays fully
  and closes, so reconnecting after completion is safe.
- The terminal event (`run.completed` / `run.failed`) is **withheld until the final result is stored**,
  so a client that sees it and immediately calls `GET /api/runs/{id}` always receives the finalized
  snapshot. Folding the streamed events reproduces the snapshot on every UI field (tested).
- A crashed run still ends with a synthesized `run.failed` (`internal_error`) and frees the slot.

## Reconciliation with `perjury/ui/contract.js` (frontend branch)
| Topic | API behaviour |
| --- | --- |
| Outcome casing | semantic outcomes are **UPPERCASE** (`PASS`, `TEST_FAIL`, `INVALID`, `TIMEOUT`, `INFRA_ERROR`) in `baseline.outcome` and `verification.original/mutant`; mutation `status` is **lowercase** (`killed`, `survived`, `invalid`, `timeout`, `infra_error`, `pending`) |
| Verification keys | `original` / `mutant` (+ `original_duration_ms`, `mutant_duration_ms`, `verdict`, `explanation`) — not `original_outcome`/`mutant_outcome` |
| Score | `score_before` / `score_after`: `{killed, survived, excluded, score, ...}` computed server-side (#25). **`score` is `null` (and `state: "inconclusive"`) when there is no valid killed/survived outcome — never 0%** |
| Comparison | new authoritative `comparison {status, delta, direction, batch_sha256, newly_killed_ids, newly_survived_ids, message}`; `delta` is `null`/`direction: "unavailable"` when either side is undefined; the browser must not recompute it |
| Result vs error | verified/rejected/inconclusive → `result {verdict, explanation, reason}`, `error: null`; failed → `error {code, message}`, `result: null` |
| `run.started` | carries `started_at_ms` (epoch ms) and `commit_sha` |
| Stages | `created, baseline, context, planning, mutation_execution, survivor_analysis, test_generation, verification, rescoring, verified/rejected/inconclusive/failed` (`context` is new) |

Additions beyond the current UI contract: `terminal`, `reason`, `candidate {candidate_path, sha256, diff}`,
`improvement_verified`, `context_sha256`, `batch_sha256`, `stages[]` (timing + evidence refs).

### Frontend follow-ups required (not done here)
1. `Score.score` must be nullable; render "n/a" instead of `pct(null)` (which shows `0%`).
2. Read the delta/direction from `comparison`; show `comparison.status == "inconsistent"` clearly.
3. Add `context` to the pipeline strip (or ignore the stage) and treat unknown event types as no-ops (already the case).
4. `mutation.started` is **not emitted** by the backend; cards go `pending` → final status.
5. Emitted but currently ignored by the reducer: `context.completed`, `execution.completed`,
   `candidate.ready`, `rescore.mutation.completed`; `candidate` may be shown as the created test file.
6. `POST /api/runs` returns 202; 409 body is `detail.message` (already handled).

## Testing
`tests/test_api.py` drives the API with injected deterministic model/execution boundaries
(no credentials/network): shapes and casing, authoritative score/comparison, null-score case, ordered
SSE, terminal-event/snapshot agreement, reconnect/resume, typed 404/409/400, concurrent starts,
failed/rejected/inconclusive/crashed runs, and credential-free import.
