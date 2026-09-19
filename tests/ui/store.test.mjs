import assert from 'node:assert/strict';
import test from 'node:test';
import { emptySnapshot, isTerminal } from '../../perjury/ui/contract.js';
import { mockEvents } from '../../perjury/ui/mock_events.js';
import { clock, deltaLabel, lastStage, pct, reduce, replay } from '../../perjury/ui/store.js';

const evs = mockEvents('r1').map((x) => x.event);
const ev = (seq, type, data, extra = {}) => ({ run_id: 'r1', seq, type, data, ...extra });

test('mock script has strictly increasing seq, one run id, and no mutation.started', () => {
  evs.forEach((e, i) => assert.equal(e.seq, i + 1));
  assert.ok(evs.every((e) => e.run_id === 'r1'));
  assert.ok(!evs.some((e) => e.type === 'mutation.started'));
  assert.ok(evs.some((e) => e.type === 'context.completed'));
});

test('mock fixture reproduces the proven canonical result', () => {
  const s = replay('r1', evs);
  assert.equal(s.stage, 'verified');
  assert.ok(isTerminal(s.stage));
  assert.equal(s.survivor_id, 'M01');
  const status = Object.fromEntries(s.mutations.map((m) => [m.id, m.status]));
  assert.equal(status.M01, 'survived');
  assert.equal(Object.values(status).filter((x) => x === 'killed').length, 5);
  assert.equal(Object.values(status).filter((x) => x === 'survived').length, 3);
  assert.equal(s.score_before.killed, 5);
  assert.equal(s.score_before.survived, 3);
  assert.equal(s.score_before.excluded, 0);
  assert.equal(s.score_before.score, 0.625);
  assert.equal(s.score_after.killed, 6);
  assert.equal(s.score_after.survived, 2);
  assert.equal(s.score_after.score, 0.75);
  assert.equal(s.comparison.delta, 0.125);
  assert.equal(s.comparison.direction, 'improved');
  assert.equal(s.comparison.status, 'confirmed');
  assert.deepEqual(s.comparison.newly_killed_ids, ['M01']);
});

test('verification uses original/mutant with UPPERCASE outcomes; statuses are lowercase', () => {
  const s = replay('r1', evs);
  assert.equal(s.verification.original, 'PASS');
  assert.equal(s.verification.mutant, 'TEST_FAIL');
  assert.equal(s.verification.mutation_id, 'M01');
  assert.equal(s.verification.verdict, 'verified');
  assert.equal('original_outcome' in s.verification, false);
  assert.equal(s.baseline.outcome, 'PASS');
  assert.ok(s.mutations.every((m) => m.status === m.status.toLowerCase()));
  assert.equal(s.result.verdict, 'verified');
  assert.equal(s.result.reason, 'verified');
  assert.equal(s.candidate.candidate_path, 'examples/refund/test_perjury_M01.py');
});

test('cards go pending -> terminal without any mutation.started event', () => {
  let s = replay('r1', evs.filter((e) => e.type !== 'mutation.completed' && e.seq <= 4));
  assert.ok(s.mutations.length === 8 && s.mutations.every((m) => m.status === 'pending'));
  s = reduce(s, ev(99, 'mutation.completed', { status: 'killed', duration_ms: 5 }, { mutation_id: 'M03', stage: 'mutation_execution' }));
  assert.equal(s.mutations.find((m) => m.id === 'M03').status, 'killed');
});

test('context stage is tracked and terminal snapshots keep the last working stage', () => {
  let s = reduce(emptySnapshot('r1'), ev(1, 'context.completed', { context_sha256: 'sha256:x' }, { stage: 'context' }));
  assert.equal(s.stage, 'context');
  assert.equal(s.context_sha256, 'sha256:x');
  s = reduce(s, ev(2, 'run.completed', { reason: 'no_actionable_survivor', result: { verdict: 'inconclusive', explanation: 'none' } }, { stage: 'inconclusive' }));
  assert.equal(lastStage(s), 'context');
  assert.equal(s.result.reason, 'no_actionable_survivor');
  assert.equal(lastStage({ ...s, stages: [{ stage: 'baseline' }, { stage: 'survivor_analysis' }] }), 'survivor_analysis');
});

test('null scores are never rendered as 0%', () => {
  assert.equal(pct(null), 'n/a');
  assert.equal(pct(undefined), 'n/a');
  assert.notEqual(pct(null), '0%');
  assert.equal(pct(0), '0%'); // a real, defined zero is still 0%
  assert.equal(pct(0.625), '62.5%');
  assert.equal(pct(0.75), '75%');
});

test('delta label is formatted from the backend comparison only', () => {
  const base = { status: 'confirmed', batch_sha256: 'x', newly_killed_ids: [], newly_survived_ids: [], message: '' };
  assert.deepEqual(deltaLabel({ ...base, delta: 0.125, direction: 'improved' }), { text: '+12.5 pts', tone: 'good' });
  assert.deepEqual(deltaLabel({ ...base, delta: -0.25, direction: 'regressed' }), { text: '−25 pts', tone: 'bad' });
  assert.deepEqual(deltaLabel({ ...base, delta: 0, direction: 'unchanged' }), { text: 'no change', tone: 'neutral' });
  assert.deepEqual(deltaLabel({ ...base, delta: null, direction: 'unavailable' }), { text: 'n/a', tone: 'neutral' });
  assert.deepEqual(deltaLabel(null), { text: 'n/a', tone: 'neutral' });
});

test('rescore.completed copies score/comparison from the payload without recomputing', () => {
  const s = reduce(emptySnapshot('r1'), ev(1, 'rescore.completed', {
    status: 'inconsistent',
    before: { killed: 1, survived: 1, excluded: 0, score: 0.123 },
    after: { killed: 0, survived: 0, excluded: 2, score: null, state: 'inconclusive' },
    delta: null, direction: 'unavailable', batch_sha256: 'b', message: 'm',
  }));
  assert.equal(s.score_before.score, 0.123); // not 0.5
  assert.equal(s.score_after.score, null);
  assert.equal(s.comparison.status, 'inconsistent');
  assert.equal(s.comparison.delta, null);
});

test('stale, duplicate and foreign-run events are ignored', () => {
  const s0 = replay('r1', evs.slice(0, 3));
  assert.equal(reduce(s0, evs[1]), s0);
  assert.equal(reduce(s0, evs[2]), s0);
  assert.equal(reduce(s0, { ...evs[5], run_id: 'other' }), s0);
});

test('run.failed is terminal and carries the error', () => {
  const s = reduce(emptySnapshot('r1'), ev(1, 'run.failed', { code: 'baseline_not_ready', message: 'baseline failed' }));
  assert.equal(s.stage, 'failed');
  assert.equal(s.error.message, 'baseline failed');
  assert.equal(s.reason, 'baseline_not_ready');
});

test('formatters', () => {
  assert.equal(clock(75000), '1:15');
  assert.equal(clock(-5), '0:00');
});
