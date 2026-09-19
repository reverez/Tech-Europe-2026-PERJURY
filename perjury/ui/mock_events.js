// @ts-check
// SIMULATED event script for UI development. Not real execution evidence.

const DIFF_M04 = `--- a/examples/refund/refund.py
+++ b/examples/refund/refund.py
@@ -7,3 +7,3 @@
-    if days_since_purchase <= 30 or premium:
+    if days_since_purchase <= 30:
         return price`;

const mut = (/** @type {string} */ id, /** @type {string} */ description, /** @type {string} */ hypothesis, /** @type {string} */ diff) =>
  ({ id, file_path: 'examples/refund/refund.py', description, hypothesis, diff });
const d = (/** @type {string} */ a, /** @type {string} */ b) =>
  `--- a/examples/refund/refund.py\n+++ b/examples/refund/refund.py\n@@ -7,3 +7,3 @@\n-${a}\n+${b}\n`;

const MUTATIONS = [
  mut('M01', 'Refund window boundary 30 -> 29', 'Day-30 purchases lose their refund.', d('    if days_since_purchase <= 30 or premium:', '    if days_since_purchase <= 29 or premium:')),
  mut('M02', 'Window comparison <= becomes <', 'Off-by-one at the boundary.', d('    if days_since_purchase <= 30 or premium:', '    if days_since_purchase < 30 or premium:')),
  mut('M03', 'Partial refund instead of full', 'Refund is halved inside the window.', d('        return price', '        return price / 2')),
  mut('M04', 'Premium customers lose out-of-window refund', 'Premium status no longer extends eligibility beyond 30 days.', DIFF_M04),
  mut('M05', 'Negative price no longer rejected', 'Validation removed.', d('    if price < 0:', '    if price < -1e18:')),
  mut('M06', 'Zero refund returns price', 'Out-of-window refunds are granted.', d('    return 0.0', '    return price')),
  mut('M07', 'Syntactically broken guard', 'Introduces an invalid expression.', d('    if days_since_purchase < 0:', '    if days_since_purchase <')),
  mut('M08', 'Unbounded loop in refund', 'Never terminates.', d('    return 0.0', '    while True: pass')),
];
const OUTCOMES = /** @type {Record<string, [string, number]>} */ ({
  M01: ['killed', 4200], M02: ['killed', 4700], M03: ['killed', 5100], M04: ['survived', 5600],
  M05: ['killed', 4400], M06: ['killed', 6100], M07: ['invalid', 900], M08: ['timeout', 10000],
});
const SCORE_BEFORE = { killed: 5, survived: 1, excluded: 2, score: 5 / 6 };
const SCORE_AFTER = { killed: 6, survived: 0, excluded: 2, score: 1 };
const TEST_CODE = `from examples.refund.refund import calculate_refund


def test_premium_customer_outside_window_still_gets_refund() -> None:
    assert calculate_refund(100.0, 45, premium=True) == 100.0
`;

/** @param {string} runId @returns {{delay:number, event:import('./contract.js').RunEvent}[]} */
export function mockEvents(runId) {
  let seq = 0;
  /** @type {{delay:number, event:import('./contract.js').RunEvent}[]} */
  const out = [];
  const add = (/** @type {number} */ delay, /** @type {string} */ type, /** @type {any} */ data, /** @type {any} */ stage, /** @type {string=} */ mutation_id) =>
    out.push({ delay, event: { run_id: runId, seq: ++seq, type, stage, mutation_id, data } });

  add(300, 'run.started', { started_at_ms: Date.now(), commit_sha: 'mock0000' }, 'baseline');
  add(1200, 'baseline.completed', { outcome: 'PASS', duration_ms: 3100, summary: '4 passed' }, 'planning');
  add(1800, 'plan.completed', { mutations: MUTATIONS }, 'mutation_execution');
  for (const m of MUTATIONS) add(80, 'mutation.started', {}, 'mutation_execution', m.id);
  for (const id of ['M07', 'M01', 'M02', 'M05', 'M03', 'M04', 'M06', 'M08']) {
    const [status, duration_ms] = OUTCOMES[id];
    add(700, 'mutation.completed', { status, duration_ms }, 'mutation_execution', id);
  }
  add(500, 'survivor.selected', {}, 'survivor_analysis', 'M04');
  add(1500, 'analysis.completed', {
    mutation_id: 'M04',
    behavioural_gap: 'No test exercises a premium customer outside the 30-day window.',
    test_intent: 'Assert premium customers past day 30 still receive a full refund.',
    possibly_equivalent: false,
    reasoning: 'Removing `or premium` changes results only for premium + days > 30, which no existing test covers.',
  }, 'test_generation');
  add(1400, 'test.proposed', {
    mutation_id: 'M04', test_name: 'test_premium_customer_outside_window_still_gets_refund',
    target_file: 'examples/refund/test_refund_premium_window.py', test_code: TEST_CODE,
    explanation: 'Pins the premium out-of-window refund rule.',
  }, 'verification');
  add(2200, 'verification.completed', {
    mutation_id: 'M04', original: 'PASS', mutant: 'TEST_FAIL', original_duration_ms: 3300, mutant_duration_ms: 3500,
  }, 'rescoring');
  add(1600, 'rescore.completed', { before: SCORE_BEFORE, after: SCORE_AFTER }, 'rescoring');
  add(300, 'run.completed', { result: { verdict: 'verified', explanation: 'Original + candidate PASS; mutant + candidate TEST_FAIL.' } }, 'verified');
  return out;
}
