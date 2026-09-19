// @ts-check
import { emptySnapshot } from './contract.js';

/** @typedef {import('./contract.js').RunSnapshot} RunSnapshot */
/** @typedef {import('./contract.js').RunEvent} RunEvent */

/**
 * Pure projection of one ordered event onto a snapshot. Stale or duplicate events
 * (seq <= last_seq) and events for another run are ignored. Terminal state is always
 * reconciled against GET /api/runs/{id} by the caller; this only drives live progress.
 * @param {RunSnapshot} snap @param {RunEvent} ev @returns {RunSnapshot}
 */
export function reduce(snap, ev) {
  if (ev.run_id !== snap.run_id || ev.seq <= snap.last_seq) return snap;
  const d = ev.data ?? {};
  const next = { ...snap, last_seq: ev.seq, stage: ev.stage ?? snap.stage };
  const patch = (/** @type {string} */ id, /** @type {object} */ p) =>
    snap.mutations.map((m) => (m.id === id ? { ...m, ...p } : m));

  switch (ev.type) {
    case 'run.started':
      next.started_at_ms = d.started_at_ms;
      next.commit_sha = d.commit_sha;
      break;
    case 'baseline.completed':
      next.baseline = d;
      break;
    case 'plan.completed':
      next.mutations = d.mutations.map((/** @type {any} */ m) => ({ ...m, status: 'pending' }));
      break;
    case 'mutation.started':
      next.mutations = patch(ev.mutation_id ?? '', { status: 'running' });
      break;
    case 'mutation.completed':
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
    case 'verification.completed':
      next.verification = d;
      break;
    case 'rescore.completed':
      next.score_before = d.before;
      next.score_after = d.after;
      break;
    case 'run.completed':
      next.result = d.result ?? null;
      break;
    case 'run.failed':
      next.error = d;
      next.stage = 'failed';
      break;
    default:
      break;
  }
  return next;
}

/** @param {string} runId @param {RunEvent[]} events */
export function replay(runId, events) {
  return events.reduce(reduce, emptySnapshot(runId));
}

/** @param {number} pct 0..1 */
export function pct(pct) {
  return `${Math.round(pct * 100)}%`;
}

/** @param {number} ms */
export function clock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
