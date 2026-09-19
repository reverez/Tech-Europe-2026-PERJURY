// Contract check: fold REAL API events through the UI reducer and compare with the REAL snapshot.
// Usage: node tests/ui/api_contract.mjs <capture.json>   (capture = {events, snapshot, ...})
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { replay, pct, deltaLabel } from '../../perjury/ui/store.js';

const { events, snapshot, expect } = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const folded = replay(snapshot.run_id, events);

for (const key of ['stage', 'last_seq', 'baseline', 'mutations', 'survivor_id', 'analysis', 'proposal',
  'verification', 'score_before', 'score_after', 'batch_sha256', 'context_sha256']) {
  assert.deepEqual(folded[key] ?? null, snapshot[key] ?? null, `reducer vs snapshot: ${key}`);
}
assert.deepEqual(folded.comparison ?? null, snapshot.comparison ?? null, 'comparison');
assert.deepEqual(folded.result ?? null, snapshot.result ?? null, 'result');
assert.deepEqual(folded.error ?? null, snapshot.error ?? null, 'error');
assert.equal(folded.candidate?.candidate_path, snapshot.candidate?.candidate_path);

// casing / shape the renderer depends on
if (snapshot.baseline) assert.match(snapshot.baseline.outcome, /^[A-Z_]+$/);
if (snapshot.verification) {
  assert.match(snapshot.verification.original, /^[A-Z_]+$/);
  assert.match(snapshot.verification.mutant, /^[A-Z_]+$/);
  assert.equal('original_outcome' in snapshot.verification, false);
}
for (const m of snapshot.mutations) assert.match(m.status, /^[a-z_]+$/);

// score display comes from the backend and null never becomes 0%
for (const sc of [snapshot.score_before, snapshot.score_after].filter(Boolean)) {
  assert.equal(pct(sc.score), sc.score == null ? 'n/a' : `${Math.round(sc.score * 1000) / 10}%`);
}
if (snapshot.comparison) {
  assert.equal(deltaLabel(snapshot.comparison).text === 'n/a', snapshot.comparison.delta == null);
}
if (expect) {
  assert.equal(snapshot.stage, expect.stage);
  if (expect.before) assert.equal(pct(snapshot.score_before.score), expect.before);
  if (expect.after !== undefined) {
    assert.equal(snapshot.score_after ? pct(snapshot.score_after.score) : null, expect.after);
  }
}
console.log('api contract ok');
