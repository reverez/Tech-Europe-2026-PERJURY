// @ts-check
// SIMULATED event script for UI development/rehearsal only. It mirrors the proven canonical refund
// result (M01 selected; first pass 5 killed / 3 survived / 0 excluded = 0.625; re-score 6 killed /
// 2 survived = 0.750) and the real #17 event shapes, but is NOT execution evidence.

const F = 'examples/refund/refund.py';
const PREMIUM = '    if days_since_purchase <= 30 or premium:';
const RET = '        return price';
const d = (/** @type {string} */ a, /** @type {string} */ b) =>
  `--- a/${F}\n+++ b/${F}\n@@ -1,1 +1,1 @@\n-${a}\n+${b}\n`;
const m = (/** @type {string} */ id, /** @type {string} */ description, /** @type {string} */ diff) =>
  ({ id, file_path: F, description, hypothesis: description, diff });

const MUTATIONS = [
  m('M01', 'Premium customers lose out-of-window refund', d(PREMIUM, '    if days_since_purchase <= 30:')),
  m('M02', 'Window boundary 30 -> 29', d(PREMIUM, '    if days_since_purchase <= 29 or premium:')),
  m('M03', 'Premium no longer sufficient alone', d(PREMIUM, '    if days_since_purchase <= 30 and premium:')),
  m('M04', 'Refund halved inside the window', d(RET, '        return price / 2')),
  m('M05', 'Out-of-window refund granted in full', d('    return 0.0', '    return price')),
  m('M06', 'Out-of-window refund becomes 1.0', d('    return 0.0', '    return 1.0')),
  m('M07', 'Negative price no longer rejected', d('    if price < 0:', '    if price < -1:')),
  m('M08', 'Inside-window refund inflated', d(RET, '        return price + 1')),
];
const FIRST_PASS = { M01: 'survived', M02: 'survived', M03: 'killed', M04: 'killed', M05: 'killed', M06: 'killed', M07: 'survived', M08: 'killed' };
const score = (/** @type {number} */ killed, /** @type {number} */ survived) => ({
  killed, survived, invalid: 0, timeout: 0, infra_error: 0, valid_total: killed + survived,
  excluded_total: 0, excluded: 0, state: 'scored', score: killed / (killed + survived),
});
const BATCH = 'sha256:' + 'ab'.repeat(32);
const CONTEXT = 'sha256:' + 'cd'.repeat(32);
const TEST_CODE = `from examples.refund.refund import calculate_refund


def test_premium_customer_outside_window_still_gets_refund() -> None:
    assert calculate_refund(100.0, 45, premium=True) == 100.0
`;

/** @param {string} runId @returns {{delay:number, event:import('./contract.js').RunEvent}[]} */
export function mockEvents(runId) {
  let seq = 0;
  /** @type {{delay:number, event:import('./contract.js').RunEvent}[]} */
  const out = [];
  const add = (/** @type {number} */ delay, /** @type {string} */ type, /** @type {string} */ stage, /** @type {any} */ data, /** @type {string=} */ mutation_id) =>
    out.push({ delay, event: { run_id: runId, seq: ++seq, type, stage: /** @type {any} */ (stage), mutation_id, data } });

  add(300, 'run.started', 'created', { started_at_ms: Date.now(), commit_sha: 'mock000' });
  add(900, 'baseline.completed', 'baseline', { outcome: 'PASS', duration_ms: 3100, summary: '4 passed' });
  add(300, 'context.completed', 'context', { context_sha256: CONTEXT });
  add(1200, 'plan.completed', 'planning', { batch_sha256: BATCH, mutations: MUTATIONS });
  for (const id of ['M07', 'M01', 'M02', 'M05', 'M03', 'M04', 'M06', 'M08']) {
    add(600, 'mutation.completed', 'mutation_execution', { status: /** @type {any} */ (FIRST_PASS)[id], outcome: 'x', duration_ms: 4200 }, id);
  }
  add(200, 'execution.completed', 'mutation_execution', { score: score(5, 3) });
  add(1200, 'analysis.completed', 'survivor_analysis', {
    mutation_id: 'M01',
    behavioural_gap: 'No test exercises a premium customer outside the 30-day window.',
    test_intent: 'Assert premium customers past day 30 still receive a full refund.',
    possibly_equivalent: false,
    reasoning: 'Removing `or premium` changes results only for premium customers after day 30, which no existing test covers.',
  }, 'M01');
  add(200, 'survivor.selected', 'survivor_analysis', { label: 'potential test gap', summary: 'Potential test gap in ' + F }, 'M01');
  add(1400, 'test.proposed', 'test_generation', {
    mutation_id: 'M01', test_name: 'test_premium_customer_outside_window_still_gets_refund',
    target_file: 'examples/refund/test_refund.py', test_code: TEST_CODE,
    explanation: 'Pins the premium out-of-window refund rule.',
  }, 'M01');
  add(900, 'candidate.ready', 'test_generation', {
    candidate_path: 'examples/refund/test_perjury_M01.py', sha256: 'ef'.repeat(32),
    diff: '--- /dev/null\n+++ b/examples/refund/test_perjury_M01.py\n',
  }, 'M01');
  add(1800, 'verification.completed', 'verification', {
    original: 'PASS', mutant: 'TEST_FAIL', original_duration_ms: 3300, mutant_duration_ms: 3500,
    verdict: 'verified', explanation: 'Generated test passes on the original and produces a normal pytest test failure on the mutant.',
  }, 'M01');
  add(1500, 'rescore.completed', 'rescoring', {
    status: 'confirmed', before: score(5, 3), after: score(6, 2), delta: 0.125, direction: 'improved',
    batch_sha256: BATCH, newly_killed_ids: ['M01'], newly_survived_ids: [],
    message: 'Same-batch re-score: 0.625 -> 0.750 (improved).',
  }, 'M01');
  add(300, 'run.completed', 'verified', {
    state: 'verified', reason: 'verified',
    result: { verdict: 'verified', explanation: 'Same-batch re-score: 0.625 -> 0.750 (improved).' },
  });
  return out;
}
