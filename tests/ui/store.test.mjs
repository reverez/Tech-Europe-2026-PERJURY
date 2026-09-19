import assert from 'node:assert/strict';
import test from 'node:test';
import { emptySnapshot, isTerminal } from '../../perjury/ui/contract.js';
import { mockEvents } from '../../perjury/ui/mock_events.js';
import { reduce, replay, pct, clock } from '../../perjury/ui/store.js';

const evs = mockEvents('r1').map((x) => x.event);

test('mock script has strictly increasing seq and one run id', () => {
  evs.forEach((e, i) => assert.equal(e.seq, i + 1));
  assert.ok(evs.every((e) => e.run_id === 'r1'));
});

test('full replay reaches verified with survivor, proof and score', () => {
  const s = replay('r1', evs);
  assert.equal(s.stage, 'verified');
  assert.ok(isTerminal(s.stage));
  assert.equal(s.survivor_id, 'M04');
  assert.equal(s.mutations.find((m) => m.id === 'M04').status, 'survived');
  assert.equal(s.verification.original, 'PASS');
  assert.equal(s.verification.mutant, 'TEST_FAIL');
  assert.equal(s.result.verdict, 'verified');
  assert.ok(s.score_after.score > s.score_before.score);
});

test('score is taken from the event payload, not recomputed', () => {
  const s = reduce(emptySnapshot('r1'), {
    run_id: 'r1', seq: 1, type: 'rescore.completed',
    data: { before: { killed: 1, survived: 1, excluded: 0, score: 0.123 }, after: { killed: 2, survived: 0, excluded: 0, score: 0.456 } },
  });
  assert.equal(s.score_before.score, 0.123);
  assert.equal(s.score_after.score, 0.456);
});

test('stale, duplicate and foreign-run events are ignored', () => {
  const s0 = replay('r1', evs.slice(0, 3));
  assert.equal(reduce(s0, evs[1]), s0);
  assert.equal(reduce(s0, evs[2]), s0);
  assert.equal(reduce(s0, { ...evs[5], run_id: 'other' }), s0);
});

test('run.failed is terminal and carries the error', () => {
  const s = reduce(emptySnapshot('r1'), { run_id: 'r1', seq: 1, type: 'run.failed', data: { code: 'baseline_red', message: 'baseline failed' } });
  assert.equal(s.stage, 'failed');
  assert.equal(s.error.message, 'baseline failed');
});

test('formatters', () => {
  assert.equal(pct(5 / 6), '83%');
  assert.equal(clock(75000), '1:15');
  assert.equal(clock(-5), '0:00');
});
