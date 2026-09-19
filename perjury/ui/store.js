// @ts-check
import { emptySnapshot, isTerminal } from './contract.js';

/** @typedef {import('./contract.js').RunSnapshot} RunSnapshot */
/** @typedef {import('./contract.js').RunEvent} RunEvent */
/** @typedef {import('./contract.js').Comparison} Comparison */

/**
 * Pure projection of one ordered event onto a snapshot. Stale or duplicate events
 * (seq <= last_seq) and events for another run are ignored. Terminal state is always
 * reconciled against GET /api/runs/{id} by the caller; this only drives live progress.
 * Scores, comparison and verdicts are copied from backend payloads, never computed here.
 * @param {RunSnapshot} snap @param {RunEvent} ev @returns {RunSnapshot}
 */
export function reduce(snap, ev) {
  if (ev.run_id !== snap.run_id || ev.seq <= snap.last_seq) return snap;
  const d = ev.data ?? {};
  const next = { ...snap, last_seq: ev.seq, stage: ev.stage ?? snap.stage };
  if (ev.stage && !isTerminal(ev.stage)) next.progress_stage = ev.stage;
  const patch = (/** @type {string} */ id, /** @type {object} */ p) =>
    snap.mutations.map((m) => (m.id === id ? { ...m, ...p } : m));

  switch (ev.type) {
    case 'run.started':
      next.started_at_ms = d.started_at_ms;
      next.commit_sha = d.commit_sha ?? undefined;
      break;
    case 'baseline.completed':
      next.baseline = d;
      break;
    case 'context.completed':
      next.context_sha256 = d.context_sha256;
      break;
    case 'plan.completed':
      next.batch_sha256 = d.batch_sha256;
      next.mutations = d.mutations.map((/** @type {any} */ m) => ({ ...m, status: 'pending' }));
      break;
    case 'mutation.completed': // cards go pending -> terminal status (no mutation.started event)
      next.mutations = patch(ev.mutation_id ?? '', { status: d.status, duration_ms: d.duration_ms });
      break;
    case 'survivor.selected':
      next.survivor_id = ev.mutation_id ?? null;
      break;
    case 'analysis.completed':
      next.analysis = d;
      break;
    case 'test.proposed':
      next.proposal = d;
      break;
    case 'candidate.ready':
      next.candidate = { mutation_id: ev.mutation_id ?? '', candidate_path: d.candidate_path, sha256: d.sha256, diff: d.diff };
      break;
    case 'verification.completed':
      next.verification = { mutation_id: ev.mutation_id ?? '', ...d };
      break;
    case 'rescore.completed':
      next.score_before = d.before;
      next.score_after = d.after;
      next.comparison = {
        status: d.status, delta: d.delta, direction: d.direction, batch_sha256: d.batch_sha256,
        newly_killed_ids: d.newly_killed_ids ?? [], newly_survived_ids: d.newly_survived_ids ?? [],
        message: d.message,
      };
      break;
    case 'run.completed':
      next.result = d.result ? { ...d.result, reason: d.reason } : null;
      next.reason = d.reason;
      break;
    case 'run.failed':
      next.error = { code: d.code, message: d.message };
      next.reason = d.code;
      next.stage = 'failed';
      break;
    default: // unknown / informational events (execution.completed, rescore.mutation.completed, ...)
      break;
  }
  return next;
}

/** @param {string} runId @param {RunEvent[]} events */
export function replay(runId, events) {
  return events.reduce(reduce, emptySnapshot(runId));
}

/** Format an authoritative 0..1 score. null/undefined is unavailable, never "0%".
 * @param {number|null|undefined} score */
export function pct(score) {
  return score == null ? 'n/a' : `${Math.round(score * 1000) / 10}%`;
}

/** Format the backend-provided comparison for display only (no arithmetic on scores).
 * @param {Comparison|null|undefined} c @returns {{text:string, tone:'good'|'bad'|'neutral'}} */
export function deltaLabel(c) {
  if (!c || c.delta == null || c.direction === 'unavailable') return { text: 'n/a', tone: 'neutral' };
  const points = Math.round(Math.abs(c.delta) * 1000) / 10;
  if (c.direction === 'improved') return { text: `+${points} pts`, tone: 'good' };
  if (c.direction === 'regressed') return { text: `−${points} pts`, tone: 'bad' };
  return { text: 'no change', tone: 'neutral' };
}

/** Last working stage reached (server `stages[]` when present, else from events).
 * @param {RunSnapshot} s */
export function lastStage(s) {
  if (s.stages?.length) return s.stages[s.stages.length - 1].stage;
  if (!isTerminal(s.stage)) return s.stage;
  return s.progress_stage ?? 'created';
}

/** @param {number} ms */
export function clock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
